# Prep

* Create GitHub repo with:
    * worker.py (the simplified worker script)
    * prompts.py (prompts file)
    * requirements.txt (system requirements)
* Update job.yaml with:
    * GitHub username/repo name
    * NRP LLM token
* Get list of all items in Islandora - outputs to `all-items.csv`

    `python full-solr_query.py`

# Deployment Steps

1. Create storage (all pvc mounts):

    `kubectl apply -f prod-storage.yaml`
    # check - volumes should say Bound
    `kubectl get pvc`

2. Deploy Redis (creates deployment and service, and a redis-queue pod)

    `kubectl apply -f prod-redis.yaml`
    # optional
    `kubectl wait --for=condition=ready pod -l app=redis-queue --timeout=300s`
    # or just check - pods should say Ready 1/1 , Status: Running
    `kubectl get pods`

3. Populate the queue (pulls `all-items.csv` and omits pids from `data/pages*.csv`)

    ***Make sure to edit populate-queue.py before running***

    # Port-forward to access Redis locally
    # In a separate terminal
    `kubectl port-forward svc/redis-service 6379:6379`

    # in original terminal - should get confirmation
    `python populate-queue.py`

4. Deploy the job

    `kubectl apply -f prod-job.yaml`
    # check - pods should say Ready 1/1 , Status: Running
    `kubectl get pods`

    # see note below about updating number of workers

    # error handling
    * see "Monitor" below
    * get get pods, copy a running pod, watch the logs in real time:
      `kubectl logs -f <podname>`

5. Monitor progress

**check current usage - IMPORTANT**

- CPU/ Memory - https://grafana.nrp-nautilus.io/d/85a562078cdf77779eaa1add43ccec1e/kubernetes-compute-resources-namespace-pods?orgId=1&from=now-1h&to=now&timezone=UTC&var-datasource=default&var-cluster=&var-namespace=edw-llm&refresh=10s
- GPU - https://grafana.nrp-nautilus.io/d/dRG9q0Ymz/k8s-compute-resources-namespace-gpus?orgId=1&from=now-30m&to=now&timezone=browser&var-namespace=edw-llm&refresh=30s
- violations: https://nrp.ai/userinfo/

**if under-utilizing or over-utilizing**

* make changes to job.yaml to update the number of workers
* stop the job, then restart. redis queue is still populated correctly, and saved work is still on pvc

    `kubectl delete job newspaper-processing`
    # Edit job.yaml: e.g, parallelism: 40, completions: 40
    `kubectl apply -f prod-job.yaml`

    # this should work to update on the fly
    `kubectl patch job newspaper-processing -p '{"spec":{"parallelism":30}}'`
    * note that this applies to workers. changes to ram/cpu will be applied to new workers but not existing ones. to change those, have to stop the job and restart (including updating the queue)

    # Monitor the job
    `kubectl get jobs -w`

    # Monitor queue status
    `python monitor_queue.py`

    # Check worker logs
    `kubectl logs -f job/newspaper-processing`

    # Monitor logs in real time
    `kubectl logs -f -l job-name=newspaper-processing-test`
    `kubectl logs -f <podname>`

# Downloading data

1. Create a temporary pod with the same PVC mounted
    `kubectl apply -f prod-mount-pvc.yaml`

2. connect to the pod (optional - to review files first):

    `kubectl exec -it temp-access -- /bin/sh`
    `ls /shared-output/`

3. run this job to concat all related csv files

    `kubectl apply -f consolidate-job.yaml`

4. wait for job to finish

    `kubectl wait --for=condition=complete job/csv-consolidator -n edw-llm --timeout=600s`
    # OR just check on job - needs to show "completed 1/1"
    `kubectl get job csv-consolidator -n edw-llm`
    # OR follow logs in real time
    `kubectl logs -f jobs/csv-consolidator`

5. if necessary to retry

    `kubectl delete job csv-consolidator` # then back to Step 3

6. download the consolidated files

    mount temp-access via Step 1 - prod-mount-pvc.yaml

    # bash
    for file in pages lp llm ads errors ed; do
    kubectl cp temp-access:/shared-output/merged_data_${file}_15.csv data/merged_data_${file}_15.csv
    done

7. cleanup temp-access

    # directories
    - already-downloaded/
    - merged-already-downloaded/
    - zOld-not-needed/

    # move files
    <!-- for fn in pages llm lp ads errors; do
        echo "$fn"
        for letter in a b c d e f g h i j k l m n o p q r s t u v w x y z; do
            echo "$letter"
            mv ${fn}_newspaper-processing-${letter}* already-downloaded/ 2>/dev/null
        done
    done -->

    # move merged files
    mv merged* merged_already_downloaded

# Download data - slow way, better for small # of files

    # Copy files locally (copies all contents of folder):
    # very slow for lots of files
    XX kubectl cp temp-access:/shared-output/ data/

    # better copy - still pretty slow, but a bit better
    # create and stream tar file directly to your local machine - no temp file needed
    # XX kubectl exec temp-access -- sh -c 'cd /shared-output && tar czf - page*.csv' > archive.tar.gz - NO GOOD - FILE LIST TOO LONG!

    kubectl exec temp-access -- sh -c 'cd /shared-output && find . -name "page*.csv" | tar czf - -T -' > pages_20251017.tar.gz
    # extract
    tar xf XX.tar.gz -C data/

# Cleanup

jobs, service, pvc, pods, deployment

* Delete the job
`kubectl delete job newspaper-processing`

* Delete Redis
`kubectl delete deployment redis-queue`
`kubectl delete service redis-service`

***CAREFUL - this removes ALL stored data***
* Delete storage
`kubectl delete pvc newspaper-outputs redis-storage`

* **CAREFUL** - force delete pod
`kubectl delete pod temp-access --grace-period=0 --force`
