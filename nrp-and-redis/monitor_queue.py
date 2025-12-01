#!/usr/bin/env python3

import redis
import time
import argparse

def monitor_progress():
    """Monitor queue progress in real-time"""
    r = redis.Redis(host='localhost', port=6379, db=0)
    queue_name = 'newspaper-jobs'

    print("Monitoring queue progress (Ctrl+C to stop)...")
    print("Time\t\tPending\tProcessing\tFailed")

    try:
        while True:
            pending = r.llen(queue_name)
            processing = r.llen(f'{queue_name}:processing')
            failed = r.llen(f'{queue_name}:failed')

            timestamp = time.strftime('%H:%M:%S')
            print(f"{timestamp}\t{pending}\t{processing}\t\t{failed}")

            if pending == 0 and processing == 0:
                print("All jobs completed!")
                break

            time.sleep(10)

    except KeyboardInterrupt:
        print("\nMonitoring stopped")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Monitor queue progress')
    args = parser.parse_args()
    monitor_progress()
