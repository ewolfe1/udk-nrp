#!/usr/bin/env python3

import redis
import json
import pandas as pd
import csv
import glob
import sys

# Read all PIDs to process
try:
    all_pids = pd.read_csv('all-items.csv')
except:
    print('all_items.csv not found. This must be created first via full-solr-query.py')
    sys.exit()

completed = set()
# OPTION A - assume all captured PIDs are complete
for file in glob.glob('data/*pages*.csv'):
    try:
        with open(file, 'r') as f:
            header = f.readline().strip().split(',')
            pid_idx = header.index('pid')
            for line in f:
                completed.add(line.split(',')[pid_idx])
    except:
        pass

# # OPTION B - 2nd pass - only PIDs with page,num,vol,or date are complete
# for file in glob.glob('data/*pages*.csv'):
#     try:
#         with open(file, 'r') as f:
#             header = f.readline().strip().split(',')
#             pid_idx = header.index('pid')
#             page_idx = header.index('page')
#             date_idx = header.index('date')
#             vol_idx = header.index('volume')
#             num_idx = header.index('number')
#
#             for line in f:
#                 parts = line.split(',')
#                 # Add if ANY of these have a value
#                 if parts[page_idx] or parts[date_idx] or parts[vol_idx] or parts[num_idx]:
#                     completed.add(parts[pid_idx])
#     except:
#         pass

# OPTION C - run all pages
# no lines to comment out

# to_skip = {f'ku-udk:{i}' for i in range(2861, 4370)}
# completed = completed | to_skip

# Filter out completed
completed = {pid.strip() for pid in completed}
to_process = all_pids[~all_pids['pid'].isin(completed)]

print(f"Total PIDs: {len(all_pids)}")
print(f"Completed: {len(completed)}")
print(f"To process: {len(to_process)}")

# print(to_process)

# Populate Redis with batching
r = redis.Redis(host='localhost', port=6379, db=0)
r.delete('newspaper-jobs')
r.delete('newspaper-jobs:processing')

# Batch insert using pipeline
BATCH_SIZE = 5000
pipe = r.pipeline()
count = 0

for _, row in to_process.iterrows():
    # print(row['pid'])
    # print(row['identifier'])
    task = {'pid': row['pid'], 'identifier': row['identifier']}
    pipe.lpush('newspaper-jobs', json.dumps(task))
    count += 1

    # Execute batch every BATCH_SIZE items
    if count % BATCH_SIZE == 0:
        pipe.execute()
        print(f"Processed {count}/{len(to_process)}")

# Execute remaining items
if count % BATCH_SIZE != 0:
    pipe.execute()

print(f"Queue populated with {len(to_process)} tasks")
