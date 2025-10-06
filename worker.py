#!/usr/bin/env python3

import pandas as pd
import numpy as np
import requests
import glob, os
from datetime import datetime
import json
from json import JSONDecodeError
from openai import OpenAI
from islandora7_rest import IslandoraClient
import cv2
from PIL import Image
import io
import base64
import layoutparser as lp
import redis
import time
import logging
import sys

# Import your prompts
import prompts

# Redis queue with improved error handling
# def get_redis_connection():
    # return redis.Redis(host='redis-service', port=6379, db=0, socket_timeout=10, socket_connect_timeout=10)

# Redis queue with improved error handling
def get_redis_connection():
    redis_host = os.environ.get('REDIS_HOST', 'redis-service')
    return redis.Redis(host=redis_host, port=6379, db=0, socket_timeout=10, socket_connect_timeout=10)

def get_next_task():
    """Get next PID from queue using BRPOPLPUSH for safety"""
    try:
        r = get_redis_connection()
        # Move from main queue to processing queue (atomic operation)
        result = r.brpoplpush('newspaper-jobs', 'newspaper-jobs:processing', timeout=60)
        if result:
            return json.loads(result.decode('utf-8'))
        else:
            # Check if both queues are empty
            main_queue_length = r.llen('newspaper-jobs')
            processing_queue_length = r.llen('newspaper-jobs:processing')
            logger.info(f"Queue status: main={main_queue_length}, processing={processing_queue_length}")

            if main_queue_length == 0 and processing_queue_length == 0:
                logger.info("All queues empty - no more work")
                return "QUEUE_EMPTY"
            elif main_queue_length == 0:
                logger.info("Main queue empty, but items still processing elsewhere")
                return "QUEUE_EMPTY"
            else:
                logger.info("Queue not empty but no task received, retrying...")
                return None

    except redis.ConnectionError as e:
        logger.error(f"Redis connection failed: {str(e)}")
        return "REDIS_ERROR"
    except Exception as e:
        logger.error(f"Redis error: {str(e)}")
        return "REDIS_ERROR"

def complete_task(task):
    """Remove completed task from processing queue"""
    try:
        r = get_redis_connection()
        task_str = json.dumps(task, sort_keys=True)
        removed = r.lrem('newspaper-jobs:processing', 1, task_str)
        if removed:
            logger.debug(f"Task {task['pid']} marked as completed")
        else:
            logger.warning(f"Could not find task {task['pid']} in processing queue")
    except Exception as e:
        logger.warning(f"Could not complete task {task.get('pid', 'unknown')}: {str(e)}")

def fail_task(task):
    """Move failed task back to main queue for potential retry"""
    try:
        r = get_redis_connection()
        task_str = json.dumps(task, sort_keys=True)
        # Remove from processing queue
        r.lrem('newspaper-jobs:processing', 1, task_str)
        # Add back to main queue for retry (optional - you could skip this)
        # r.lpush('newspaper-jobs', task_str)
        logger.debug(f"Task {task['pid']} marked as failed")
    except Exception as e:
        logger.warning(f"Could not fail task {task.get('pid', 'unknown')}: {str(e)}")

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Setup Islandora client
isURL = "https://digital.lib.ku.edu/islandora/rest"
is_client = IslandoraClient(isURL)

try:
    is_client.solr_query('PID:*root')
    logger.info('Islandora client working okay')
except Exception as e:
    logger.error(f'Islandora client not connecting to REST: {str(e)}')
    sys.exit(1)

# Setup LLM
key = os.environ.get('LLM_KEY')
if not key:
    logger.error('LLM_KEY environment variable not set')
    sys.exit(1)

client = OpenAI(api_key=key, base_url="https://ellm.nrp-nautilus.io/v1")
llm_model = 'glm-v'

# Test LLM connection
try:
    completion = client.chat.completions.create(
        model=llm_model,
        messages=[{"role": "system", "content": ""},
                 {"role": "user", "content": "Just checking to see if you're awake."}])
    logger.info('LLM connection successful')
except Exception as e:
    logger.error(f'LLM connection failed: {str(e)}')
    sys.exit(1)

# Load layoutparser model
def load_newspaper_navigator():
    config_path = 'lp://NewspaperNavigator/faster_rcnn_R_50_FPN_3x/config'
    return lp.models.Detectron2LayoutModel(
        config_path=config_path,
        extra_config=["MODEL.ROI_HEADS.SCORE_THRESH_TEST", 0.5],
        enforce_cpu=False
    )

logger.info("Loading layoutparser model...")
try:
    lp_model = load_newspaper_navigator()
    logger.info("Layoutparser model loaded successfully")
except Exception as e:
    logger.error(f"Failed to load layoutparser model: {str(e)}")
    sys.exit(1)

# Your original functions (unchanged)
def filter_lp(results):
    max_items = {}
    for item in results:
        key = (item['x_1'], item['y_1'], item['x_2'], item['y_2'])
        if key not in max_items or item['score'] > max_items[key]['score']:
            max_items[key] = item
    return list(max_items.values())

def run_lp(pid, identifier):
    url = f'https://digital.lib.ku.edu/islandora/object/{pid}/datastream/JP2/view'
    response = requests.get(url, timeout=60)  # Add timeout
    response.raise_for_status()  # Raise exception for HTTP errors

    image = Image.open(io.BytesIO(response.content))
    if image.mode != 'RGB':
        image = image.convert('RGB')
    image_for_lp = np.array(image)
    layout = lp_model.detect(image_for_lp)

    results = []
    for l in layout:
        results.append({
            'x_1': l.block.x_1, 'y_1': l.block.y_1, 'x_2': l.block.x_2, 'y_2': l.block.y_2,
            'score': l.score, 'type': l.type,
            'identifier': identifier, 'pid': pid,
        })

    results = filter_lp(results)
    return results, image

def parse_dates(s):
    try:
        if '_to_' in s:
            parts = s.split('_to_')
            start = datetime.strptime(parts[0].replace('_', '/'), '%m/%d/%Y')
            end = datetime.strptime(parts[1].replace('_', '/'), '%m/%d/%Y')
            return start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')
        elif len(s.split('_')) == 3:
            _, start_str, end_str = s.split('_')
            start = datetime.strptime(start_str, '%m-%d-%Y')
            end = datetime.strptime(end_str, '%m-%d-%Y')
            return start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')
        else:
            _, start_str, end_str = s.split('-')
            start = datetime.strptime(start_str, '%Y%m%d')
            end = datetime.strptime(end_str, '%Y%m%d')
            return start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')
    except ValueError as e:
        logger.warning(f'Unknown date format: {s}, error: {str(e)}')
        return None, None

def crop_and_encode(image, header=False, coords=None):
    if header:
        w, h = image.size
        crop_h = int(h * 0.15)
        img_crop = image.crop((0, 0, w, crop_h))
    elif coords:
        img_crop = image.crop((coords['x_1'], coords['y_1'], coords['x_2'], coords['y_2']))
    else:
        img_crop = image

    buffer = io.BytesIO()
    img_crop.save(buffer, format='JPEG', quality=85)
    img_enc = base64.b64encode(buffer.getvalue()).decode('utf-8')
    return img_enc

def decode_message(message):
    try:
        text = message.content[0].text
    except:
        text = message

    to_strip = [r'json\n', '<|end_of_box|>', '<|start_of_box|>',
                '<think>', '</think>', '```json', '```']

    for t in to_strip:
        try:
            text = text.strip().replace(t, '')
        except (IndexError, AttributeError):
            continue

    cleaned = text.replace('\n', '')

    if cleaned and cleaned[0] != '{':
        cleaned = '{' + cleaned
    if cleaned and not cleaned.endswith('}'):
        cleaned = cleaned + '}'

    for i, char in enumerate(cleaned):
        if char == '{':
            bracket_count = 0
            for j in range(i, len(cleaned)):
                if cleaned[j] == '{':
                    bracket_count += 1
                elif cleaned[j] == '}':
                    bracket_count -= 1
                    if bracket_count == 0:
                        candidate = cleaned[i:j+1]
                        try:
                            data = json.loads(candidate)
                            return data
                        except JSONDecodeError:
                            logger.warning(f'JSON decode error: {candidate}')
                            return candidate
    return cleaned

def llm_query(pid, identifier, date, image, header=False, coords=None):
    if header:
        img_enc = crop_and_encode(image, header=True)
        url = f"data:image/jpeg;base64,{img_enc}"
        sys_prompt = prompts.page_prompt()
    elif coords:
        img_enc = crop_and_encode(image, coords=coords)
        url = f"data:image/jpeg;base64,{img_enc}"
        sys_prompt = prompts.ad_prompt()
    else:
        url = f'https://digital.lib.ku.edu/islandora/object/{pid}/datastream/JP2/view'
        sys_prompt = prompts.item_prompt()

    text = """Process this image according to system directions."""
    if date:
        text += f"Likely date/date range for this item is {date}."

    completion = client.chat.completions.create(
        model=llm_model,
        messages=[
            {"role": "system", "content": sys_prompt},
            {
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": text
                },
                {"type": "image_url",
                 "image_url": {"url": url}}]
            },
            {"role": "assistant", "content": "{"}
        ],
    )

    msg = completion.choices[0].message.content
    return decode_message(msg)

# Setup output files
worker_id = os.environ.get('HOSTNAME', 'worker-unknown')
timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

# Ensure output directory exists
os.makedirs('/shared-output', exist_ok=True)

output_files = {
    'lp_items': f'/shared-output/lp_items_{worker_id}_{timestamp}.csv',
    'pages': f'/shared-output/pages_{worker_id}_{timestamp}.csv',
    'llm_items': f'/shared-output/llm_items_{worker_id}_{timestamp}.csv',
    'ads': f'/shared-output/ads_{worker_id}_{timestamp}.csv',
    'errors': f'/shared-output/errors_{worker_id}_{timestamp}.csv',
}

logger.info(f"Worker {worker_id} will save results to: {output_files['lp_items']}")

def save_results():
    """Save current results to CSV"""
    for data in [(lp_results, 'lp_items'),(page_results,'pages'),
        (llm_item_results,'llm_items'),(ad_results,'ads'),(error_results,'errors')]:
        if data[0]:
            pd.DataFrame(data[0]).to_csv(output_files[data[1]], index=False)
        logger.info(f"Saved {len(data[0])} {data[1]}")

    logger.info(f"Results saved successfully")


# Main processing loop
logger.info(f"Worker {worker_id} starting...")
processed_count = 0
error_count = 0
consecutive_errors = 0

# Initialize result lists
lp_results = []
page_results = []
llm_item_results = []
ad_results = []
error_results = []

while True:
    try:
        # Get next task
        task = get_next_task()

        if task == "QUEUE_EMPTY":
            logger.info("Queue is empty, worker exiting")
            break
        elif task == "REDIS_ERROR":
            logger.error("Redis connection issues, worker exiting")
            sys.exit(1)
        elif task is None:
            logger.info("No tasks available, waiting...")
            time.sleep(10)  # Wait before checking again
            continue

        pid = task['pid']
        identifier = task['identifier']

        logger.info(f"Processing {pid} (task {processed_count + 1})")

        try:
            # layout parser
            lp_data, image = run_lp(pid, identifier)

            # LLM data
            start_date, end_date = parse_dates(identifier.split('/')[0])
            date_range = f"{start_date}-{end_date}" if start_date and end_date else "unknown"

            # Page metadata - header
            page_query = llm_query(pid, identifier, date_range, image, header=True)
            date = page_query.get('date', date_range)

            # LLM items
            llm_item_query = llm_query(pid, identifier, start_date, image)

            # Ads
            lp_ads = [d for d in lp_data if d['type'] == 6]
            xy_coords = ['x_1', 'x_2', 'y_1', 'y_2']

            # Store results
            lp_results.extend(lp_data)
            page_results.append({'pid': pid, "identifier": identifier, **page_query})

            if len(llm_item_query.get('items', [])) > 0:
                for item in llm_item_query['items']:
                    llm_item_results.append({'pid': pid, "identifier": identifier, **item})

            if len(lp_ads) == 0:
                ad_results.append({'pid': pid, 'identifier': identifier, 'error': 'No ads found by LLM'})
            else:
                for ad_dict in lp_ads:
                    ad_coords = {k: ad_dict[k] for k in xy_coords if k in ad_dict}
                    ad_query = llm_query(pid, identifier, start_date, image, coords=ad_coords)
                    ad_results.append({'pid': pid, "identifier": identifier, **ad_coords, **ad_query})

            # Mark task as completed
            complete_task(task)

            processed_count += 1
            consecutive_errors = 0  # Reset error counter on success
            logger.info(f"Successfully processed {pid} ({processed_count} total)")

            # optional logging to keep running count
            for data in [(lp_results, 'lp_items'),(page_results,'pages'),
                (llm_item_results,'llm_items'),(ad_results,'ads'),(error_results,'errors')]:
                if data[0]:
                    logger.info(f"  -- Current count: {len(data[0])} {data[1]}")

        except Exception as e:
            error_count += 1
            consecutive_errors += 1
            logger.error(f"Error processing {pid}: {str(e)}")

            error_results.append({
                'pid': pid,
                'identifier': identifier,
                'error': str(e),
                'timestamp': datetime.now().isoformat()
            })

            # Mark task as failed (removes from processing queue)
            fail_task(task)

            # Exit if too many consecutive errors (possible system issue)
            if consecutive_errors >= 10:
                logger.error("Too many consecutive errors, worker exiting")
                break

        # Save results periodically
        if processed_count % 10 == 0 and processed_count > 0:  # Save every 10 images
            save_results()
            logger.info(f"Saved results after {processed_count} images")

            # reset lists to keep memory free
            lp_results = []
            page_results = []
            llm_item_results = []
            ad_results = []
            error_results = []

    except KeyboardInterrupt:
        logger.info("Worker interrupted by user")
        break
    except Exception as e:
        logger.error(f"Unexpected error in main loop: {str(e)}")
        time.sleep(10)  # Wait before retrying

# Final save and summary
logger.info("Saving final results...")
save_results()
logger.info(f"Worker {worker_id} completed. Processed: {processed_count}, Errors: {error_count}")

# Final queue status check
try:
    r = get_redis_connection()
    main_remaining = r.llen('newspaper-jobs')
    processing_remaining = r.llen('newspaper-jobs:processing')
    logger.info(f"Final queue status: main={main_remaining}, processing={processing_remaining}")
except:
    pass

logger.info(f"Worker {worker_id} exiting")
