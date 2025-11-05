#!/usr/bin/env python3

import pandas as pd
import numpy as np
import requests
import glob, os
import random
from datetime import datetime
import json
from json import JSONDecodeError
from openai import OpenAI
from islandora7_rest import IslandoraClient
import cv2
import torch
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
        # if removed:
        #     logger.debug(f"Task {task['pid']} marked as completed")
        # else:
        #     logger.warning(f"Could not find task {task['pid']} in processing queue")
    except Exception as e:
        logger.warning(f"Could not complete task {task.get('pid', 'unknown')}: {str(e)}")

def fail_task(task):
    """Move failed task back to main queue for potential retry"""
    try:
        r = get_redis_connection()
        task_str = json.dumps(task, sort_keys=True)
        # Remove from processing queue
        r.lrem('newspaper-jobs:processing', 1, task_str)
        # Add back to main queue for retry (optional)
        r.lpush('newspaper-jobs', task_str)
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

client = OpenAI(api_key=key, base_url="https://ellm.nrp-nautilus.io/v1", max_retries=0)
llm_model = 'glm-v' # 10/25 not working well on nrp?
# llm_model = 'gemma3'

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

# START - comment out to skip layoutparser (1 of 3)
# # Load layoutparser model
# def load_newspaper_navigator():
#     config_path = 'lp://NewspaperNavigator/faster_rcnn_R_50_FPN_3x/config'
#     device = 'cuda' if torch.cuda.is_available() else 'cpu'
#     return lp.models.Detectron2LayoutModel(
#         config_path=config_path,
#          extra_config=["MODEL.ROI_HEADS.SCORE_THRESH_TEST", 0.5],
#          device=device
#         )
#
# logger.info("Loading layoutparser model...")
# try:
#     lp_model = load_newspaper_navigator()
#     logger.info("Layoutparser model loaded successfully")
# except Exception as e:
#     logger.error(f"Failed to load layoutparser model: {str(e)}")
#     sys.exit(1)

# END - comment out to skip layoutparser


# highlight specific columns from lp
def filter_lp(results):
    max_items = {}
    for item in results:
        key = (item['x_1'], item['y_1'], item['x_2'], item['y_2'])
        if key not in max_items or item['score'] > max_items[key]['score']:
            max_items[key] = item
    return list(max_items.values())

def get_image(pid, max_retries=5):

    url = f'https://digital.lib.ku.edu/islandora/object/{pid}/datastream/OBJ/view'

    # Retry loop for GET request
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=60)
            response.raise_for_status()
            image = Image.open(io.BytesIO(response.content))
            if image.mode != 'RGB':
                image = image.convert('RGB')
            return image
        except Exception as e:
            if attempt == max_retries - 1:  # Last attempt
                raise
            time.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s, 4s


def run_lp(pid, identifier):

    image = get_image(pid)
    results = []
    # START - comment out to skip layoutparser (2 of 3)
    # image_for_lp = np.array(image)
    # layout = lp_model.detect(image_for_lp)
    #
    # for l in layout:
    #     results.append({
    #             'x_1': l.block.x_1, 'y_1': l.block.y_1, 'x_2': l.block.x_2, 'y_2': l.block.y_2,
    #             'score': l.score, 'type': l.type,
    #             'identifier': identifier, 'pid': pid,
    #             })
    #
    # results = filter_lp(results)
    # END - comment out to skip layoutparser
    return results, image

def parse_dates(s):
    try:
        if len(s.split('_')) == 3:
            _, start_str, end_str = s.split('_')
            start = datetime.strptime(start_str, '%m-%d-%Y')
            end = datetime.strptime(end_str, '%m-%d-%Y')
            return start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')
        elif len(s.split('_')) == 7:
            _, start_m, start_d, start_y, end_m, end_d, end_y = s.split('_')
            start = datetime(int(start_y), int(start_m), int(start_d))
            end = datetime(int(end_y), int(end_m), int(end_d))
            return start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')
        elif '_to_' in s:
            parts = s.split('_to_')
            start = datetime.strptime(parts[0].replace('_', '/'), '%m/%d/%Y')
            end = datetime.strptime(parts[1].replace('_', '/'), '%m/%d/%Y')
            return start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')
        else:
            _, start_str, end_str = s.split('-')
            start = datetime.strptime(start_str, '%Y%m%d')
            end = datetime.strptime(end_str, '%Y%m%d')
            return start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')
    except ValueError as e:
        logger.warning(f'Unknown date format: {s}, error: {str(e)}')
        return None, None

def encode_img(image):
    buffer = io.BytesIO()
    image.save(buffer, format='JPEG', quality=95, optimize=True, subsampling=0)
    buffer.seek(0)
    return base64.b64encode(buffer.read()).decode("utf-8")

def crop_and_encode(image, header=False, coords=None):
    if header:
        w, h = image.size
        # COMMENT OUT ONE OF THESE - A or B
        # A: look at header only
        crop_top_15 = int(h * 0.15)
        img = image.crop((0, 0, w, crop_top_15))

        # # B: look at footer only
        # crop_bottom_15 = int(h * 0.85)
        # img = image.crop((0, crop_bottom_15, w, h))
    elif coords:
        img = image.crop((coords['x_1'], coords['y_1'], coords['x_2'], coords['y_2']))
    else:
        img = image
    if img.mode in ('RGBA', 'LA', 'P'):
        img = img.convert('RGB')

    max_file_size = 3670016  # 3.5MB
    max_size = 4000 # pixel length

    # Try original image first
    image_encode = encode_img(img)
    image_encode_size = len(image_encode)

    if image_encode_size <= max_file_size:
        logger.info(f"Image size OK: {image_encode_size / (1024 * 1024):.2f}MB")
        return image_encode

    while max_size >= 100:
        # Calculate new dimensions
        width, height = img.size
        scale = max_size / max(width, height)

        if scale >= 1:
            resized_img = img
        else:
            new_width = int(width * scale)
            new_height = int(height * scale)
            resized_img = img.resize((new_width, new_height), Image.LANCZOS)

        image_encode = encode_img(resized_img)
        logger.info(f'Resized image: {len(image_encode)/(1024*1024):.2f}MB')

        # Check size
        if len(image_encode) <= max_file_size:
            return image_encode

        # Calculate next size
        size_ratio = max_file_size / len(image_encode)
        max_size = int(max_size * (size_ratio ** 0.5) * 0.93)

    return image_encode

def decode_message(message):
    try:
        text = message.content[0].text
    except:
        text = message

    to_strip = [r'json\n', '<|end_of_box|>', '<|start_of_box|>','<|begin_of_box|>',
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

def llm_query(pid, identifier, date, image, header=False, coords=None, max_retries=5):
    """LLM query with retry logic and rate limiting"""

    # Determine prompt and image based on query type
    if header:
        img_enc = crop_and_encode(image, header=True)
        url = f"data:image/jpeg;base64,{img_enc}"
        sys_prompt = prompts.page_prompt()
    elif coords:
        if coords[0] == 'ads':
            sys_prompt = prompts.ad_prompt()
        else:
            sys_prompt = prompts.ed_comics_prompt()
        img_enc = crop_and_encode(image, coords=coords[1])
        url = f"data:image/jpeg;base64,{img_enc}"
    else:

        # url = f'https://digital.lib.ku.edu/islandora/object/{pid}/datastream/OBJ/view'
        # alt method of sending pre-encoded image
        img_enc = crop_and_encode(image)
        url = f"data:image/jpeg;base64,{img_enc}"
        sys_prompt = prompts.item_prompt()

    text = """Process this image according to system directions."""
    if date:
        text += f"Likely date/date range for this item is {date}."

    # Retry loop with exponential backoff
    for attempt in range(max_retries):
        try:
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

            # Add small delay between successful calls to avoid hammering LLM
            # time.sleep(0.5)
            # test for valid json
            try:
                result = json.loads(msg)
                result['model'] = completion.model
                return result
            except JSONDecodeError:
                decoded_msg = decode_message(msg)
                decoded_msg['model'] = completion.model
                return decoded_msg

        except Exception as e:
            error_str = str(e)
            base_delay = 2

            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                logger.warning(f"LLM error for {pid} (attempt {attempt+1}/{max_retries}), retrying in {delay:.1f}s: {error_str}")
                time.sleep(delay)
                continue
            # Non-retryable error or out of retries
            raise

    raise Exception(f"LLM query failed after {max_retries} attempts")

def log_error(pid, identifier, e, task, error_count, consecutive_errors):
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
    return consecutive_errors

# START - DELETE THIS WHEN DONE
lp_df = pd.read_csv('/shared-output/merged-already-downloaded/merged_data_lp_05.csv')
# END - DELETE THIS

# Setup output files
worker_id = os.environ.get('HOSTNAME', 'worker-unknown')

# Ensure output directory exists
os.makedirs('/shared-output', exist_ok=True)

output_files = {
    'lp_items': '/shared-output/lp_items_{}_{}.csv',
    'pages': '/shared-output/pages_{}_{}.csv',
    'llm_items': '/shared-output/llm_items_{}_{}.csv',
    'ads': '/shared-output/ads_{}_{}.csv',
    'ed_comics': '/shared-output/ed_comics_{}_{}.csv',
    'errors': '/shared-output/errors_{}_{}.csv',
}

def save_results():
    """Save current results to CSV"""

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S%f')

    for data in [(lp_results, 'lp_items'),(page_results,'pages'),
        (llm_item_results,'llm_items'),(ad_results,'ads'),
        (edc_results,'ed_comics'),(error_results,'errors')]:
        if data[0]:
            fn = output_files[data[1]].format(worker_id, timestamp)
            pd.DataFrame(data[0]).to_csv(fn, index=False)
            logger.info(f"Saved {len(data[0])} {fn}")

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
edc_results = []
error_results = []
tasks_in_process = []

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

        # putting try/except here, since run_lp() is the funct that
        # pulls the img from Islandora
        try:
            # layout parser
            lp_data, image = run_lp(pid, identifier)
        except Exception as e:
            logger.info(e)
            consecutive_errors = log_error(pid, identifier, e, task, error_count, consecutive_errors)
            break
        # llm queries
        try:
            # LLM data
            start_date, end_date = parse_dates(identifier.split('/')[0])
            date_range = f"{start_date}-{end_date}" if start_date and end_date else "unknown"

            # START - comment out to skip page-level LLM (1 of 1)
            Page metadata - header
            page_query = llm_query(pid, identifier, date_range, image, header=True)
            date = page_query.get('date', date_range)
            page_results.append({'pid': pid, "identifier": identifier, **page_query})
            # END - comment out to skip page-level LLM (1 of 1)

            # Store results
            # START - comment out to skip layoutparser (3 of 3)
            # lp_results.extend(lp_data)
            # END - comment out to skip layoutparser


            # START - comment out to skip item-level LLM (1 of 1)
            # LLM items
            # llm_item_query = llm_query(pid, identifier, start_date, image)
            # if len(llm_item_query.get('items', [])) > 0:
            #     for item in llm_item_query['items']:
            #         llm_item_results.append({'pid': pid, "identifier": identifier, **item})
            # END - comment out to skip item-level LLM

            # START - comment out to skip ads via LLM (requires layoutparser) (1 of 1)
            # # Ads
            # lp_ads = [d for d in lp_data if d['type'] == 6]
            # xy_coords = ['x_1', 'x_2', 'y_1', 'y_2']
            #
            # if len(lp_ads) == 0:
            #     ad_results.append({'pid': pid, 'identifier': identifier, 'error': 'No ads found by LLM'})
            # else:
            #     for ad_dict in lp_ads:
            #         ad_coords = {k: ad_dict[k] for k in xy_coords if k in ad_dict}
            #         ad_query = llm_query(pid, identifier, start_date, image, coords=('ads',ad_coords))
            #         ad_results.append({'pid': pid, "identifier": identifier, **ad_coords, **ad_query})
            # END - comment out to skip ads

            # START - comment out to skip editorial comics via LLM (requires layoutparser) (1 of 1)
            # editorial comics
            lp_data = lp_df[lp_df.pid==pid]
            lp_edc = [d for d in lp_data if d['type'] == 4]
            xy_coords = ['x_1', 'x_2', 'y_1', 'y_2']

            if len(lp_edc) == 0:
                pass
                # edc_results.append({'pid': pid, 'identifier': identifier, 'error': 'No editorial comics found by LP'})
            else:
                for edc_dict in lp_edc:
                    edc_coords = {k: edc_dict[k] for k in xy_coords if k in edc_dict}
                    edc_query = llm_query(pid, identifier, start_date, image, coords=('edc',edc_coords))
                    edc_results.append({'pid': pid, "identifier": identifier, **edc_coords, **edc_query})
            # END - comment out to skip editorial comics

            processed_count += 1
            consecutive_errors = 0  # Reset error counter on success
            logger.info(f"Successfully processed {pid} ({processed_count} total)")

            # optional logging to keep running count
            for data in [(lp_results, 'lp_items'),(page_results,'pages'),
                (llm_item_results,'llm_items'),(ad_results,'ads'),
                (edc_results,'ed_comics'),(error_results,'errors')]:
                if data[0]:
                    logger.info(f"  -- Current count: {len(data[0])} {data[1]}")

        except Exception as e:
            consecutive_errors = log_error(pid, identifier, e, task, error_count, consecutive_errors)
            logger.info(e)
            if consecutive_errors >= 10:
                logger.error("Too many consecutive errors, exiting")
                break
            continue

        if ct % 50 != 0:

            # Save results
            save_results()

            # Mark task as completed
            for tasks_in_process:
                complete_task(task)

            # reset lists to keep memory free
            lp_results = []
            page_results = []
            llm_item_results = []
            ad_results = []
            edc_results = []
            error_results = []
            tasks_in_process = []

    except KeyboardInterrupt:
        logger.info("Worker interrupted by user")
        break
    except Exception as e:
        logger.error(f"Unexpected error in main loop: {str(e)}")
        time.sleep(10)  # Wait before retrying

# Final save and summary
logger.info("Saving final results...")
save_results()
# Mark task as completed
for tasks_in_process:
    complete_task(task)
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
