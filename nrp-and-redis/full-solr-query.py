#!/usr/bin/env python3

import json
from islandora7_rest import IslandoraClient
import pandas as pd
import sys
import os

# Connect to Islandora
isURL = $ISLANDORA_URL
is_client = IslandoraClient(isURL)

# Test connection
try:
    is_client.solr_query('PID:*root')
    print("✓ Islandora connection successful")
except Exception as e:
    print(f"✗ Islandora connection failed: {e}")
    sys.exit()

# Set query
query = 'PID:$COLL_NS\\:{}* AND RELS_EXT_hasModel_uri_ms:"info:fedora/islandora:pageCModel"'
fields = ['PID','mods_identifier_local_displayLabel_ms','RELS_EXT_hasModel_uri_ms']

item_file = 'all-items.csv'

if os.path.isfile(item_file):

    existing_df = pd.read_csv(item_file)
    all_items = existing_df.to_dict('records')
    completed = set(existing_df['pid'].tolist())
    print(f'{len(completed)} items already found')
else:
    all_items = []
    completed = set()

count = 0
for i in range(10,99):
# for i in [269]:

    print(i)

    for item in is_client.solr_generator(query.format(i), fl=fields):

        try:
            if item['PID'] not in completed:
                all_items.append({'pid': item['PID'],
                    'identifier': item['mods_identifier_local_displayLabel_ms'][0]
                    })
                completed.add(item['PID'])
                count += 1
                if count % 1000 == 0:
                    print(count)
        except Exception as e:
            print(item)
            print(e)
            pd.DataFrame(all_items).to_csv(item_file, index=False)
            break

print(f"{len(all_items)} retrieved by solr_generator")
# not all were caught for some reason, so scooping up missed ones
# probably just an internal solr limit
# note that this does query some legit missing pids, e.g., deleted items
existing_numbers = {int(s.split(':')[1]) for s in completed}
# range of integers ending in the highest PID
complete_set = set(range(1, 200656))
missing = sorted(complete_set - existing_numbers)

print(f"{len(missing)} not found in first query. Starting second query.")

for m in missing:
    print(f'PID:"$COLL_NS:{m}"')
    res = is_client.solr_query(f'PID:"$COLL_NS:{m}" AND RELS_EXT_hasModel_uri_ms:"info:fedora/islandora:pageCModel"')

    if res['response']['numFound']>0:
        item =  res['response']['docs'][0]
        try:
            if 'book' in  item['RELS_EXT_hasModel_uri_ms']:
                print(f"$COLL_NS:{m} is a book")
            else:
                all_items.append({'pid': item['PID'],
                    'identifier': item['mods_identifier_local_displayLabel_ms'][0]
                    })
                completed.add(item['PID'])
                count += 1
        except Exception as e:
            print(item)
            print(e)

print(f"Done - {len(all_items)} collected")
pd.DataFrame(all_items).to_csv(item_file, index=False)
