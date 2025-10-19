#!/usr/bin/env python3

import pandas as pd
import numpy as np
import requests
import glob, os
import random
from datetime import datetime
import json
from json import JSONDecodeError
from islandora7_rest import IslandoraClient
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
    redis_host = os.environ.get('REDIS_HOST', 'redis-service-lp')
    return redis.Redis(host=redis_host, port=6379, db=0, socket_timeout=10, socket_connect_timeout=10)

def get_next_task():
    """Get next PID from queue using BRPOPLPUSH for safety"""
    try:
        r = get_redis_connection()
        # Move from main queue to processing queue (atomic operation)
        result = r.brpoplpush('newspaper-jobs-lp', 'newspaper-jobs-lp:processing', timeout=60)
        if result:
            return json.loads(result.decode('utf-8'))
        else:
            # Check if both queues are empty
            main_queue_length = r.llen('newspaper-jobs-lp')
            processing_queue_length = r.llen('newspaper-jobs-lp:processing')
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
        removed = r.lrem('newspaper-jobs-lp:processing', 1, task_str)
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
        r.lrem('newspaper-jobs-lp:processing', 1, task_str)
        # Add back to main queue for retry (optional)
        r.lpush('newspaper-jobs-lp', task_str)
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

# Load layoutparser model
def load_newspaper_navigator():
    config_path = 'lp://NewspaperNavigator/faster_rcnn_R_50_FPN_3x/config'
    device = 'cuda' if torch.cuda.is_available() else 'cpu'

    return lp.models.Detectron2LayoutModel(
        config_path=config_path,
        extra_config=["MODEL.ROI_HEADS.SCORE_THRESH_TEST", 0.5],
        device=device
    )

logger.info("Loading layoutparser model...")
try:
    lp_model = load_newspaper_navigator()
    logger.info("Layoutparser model loaded successfully")
except Exception as e:
    logger.error(f"Failed to load layoutparser model: {str(e)}")
    sys.exit(1)

# highlight specific columns from lp
def filter_lp(results):
    max_items = {}
    for item in results:
        key = (item['x_1'], item['y_1'], item['x_2'], item['y_2'])
        if key not in max_items or item['score'] > max_items[key]['score']:
            max_items[key] = item
    return list(max_items.values())

def run_lp(pid, identifier):
    # Return 'JP2' if available, otherwise 'OBJ' as fallback
    try:
        url = f'https://digital.lib.ku.edu/islandora/object/{pid}/datastream/JP2/view'
        r = requests.head(url, timeout=5, allow_redirects=True)
        if r.status_code != 200:
            url = f'https://digital.lib.ku.edu/islandora/object/{pid}/datastream/OBJ/view'
    except Exception as e:
        url = f'https://digital.lib.ku.edu/islandora/object/{pid}/datastream/OBJ/view'
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
    return results

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

# Setup output files
worker_id = os.environ.get('HOSTNAME', 'worker-unknown')

# Ensure output directory exists
os.makedirs('/shared-output', exist_ok=True)

output_files = {
    'lp_items': '/shared-output/lp_items_{}_{}.csv',
    'errors': '/shared-output/errors_{}_{}.csv',
}

def save_results():
    """Save current results to CSV"""

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S%f')

    for data in [(lp_results, 'lp_items'),(error_results,'errors')]:
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

        # putting try/except here, since run_lp() is the funct that
        # pulls the img from Islandora
        try:
            # layout parser
            lp_data = run_lp(pid, identifier)

            # Store results
            lp_results.extend(lp_data)

            processed_count += 1
            consecutive_errors = 0  # Reset error counter on success
            logger.info(f"Successfully processed {pid} ({processed_count} total)")

            # optional logging to keep running count
            for data in [(lp_results, 'lp_items'),(error_results,'errors')]:
                if data[0]:
                    logger.info(f"  -- Current count: {len(data[0])} {data[1]}")

        except Exception as e:
            consecutive_errors = log_error(pid, identifier, e, task, error_count, consecutive_errors)
            logger.info(e)
            break

        # Save results
        save_results()

        # Mark task as completed
        complete_task(task)

        # reset lists to keep memory free
        lp_results = []
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
    main_remaining = r.llen('newspaper-jobs-lp')
    processing_remaining = r.llen('newspaper-jobs-lp:processing')
    logger.info(f"Final queue status: main={main_remaining}, processing={processing_remaining}")
except:
    pass

logger.info(f"Worker {worker_id} exiting")
