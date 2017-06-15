#
# Copyright 2017 Melon Software Ltd (UK), all rights reserved
#
import logging
from collections import namedtuple

import boto3
import json
import re
from hashlib import md5
from time import sleep
from typing import List

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sfn_client = boto3.client('stepfunctions')
batch_client = boto3.client('batch')


Arn = namedtuple('Arn', ['partition', 'service', 'region', 'account', 'resourcetype', 'resource'])
BatchJob = namedtuple('BatchJob', ['id', 'name'])
BatchRecord = namedtuple('BatchRecord', ['execution_id', 'batch_job', 'task_token', 'status'])


def handler_schedule(event, context):
    """
    Called when the a Step Function requests that one or more tasks be scheduled as Batch jobs.  This expects an `event`
    input in the following form:
    
    {
        "Meta": {
            "ExecutionArn": <STRING>,
            "ActivityArn": <STRING>,
            "Environment": <STRING>
        },
        "Input": <OBJECT>,
        "Branches": [
            {
                "Resource": {
                    "BatchJobDefinition": <ARN>,
                    "BatchJobQueue": <ARN>
                },
                "InputPath": <PATHSPEC>
            }
        ]
    }
    
    Where for each branch, <PATHSPEC> is a reference into the `Input` object to get the path for that batch invocation.
    
    The `Resource` ARN must be an AWS Batch Job Definition.  
    
    `ExecutionArn` is probably *not* the *actual* ARN of the execution, but any unique value will do.
    
    If your input data is not in this format, there are various other  possible entry points which will munge it for you
    
    :param event:
    :param context:
    :return:
    """
    logger.info(json.dumps(event))
    validate_meta(event)
    execution_id = str(event['Meta']['ExecutionArn'])
    activity_arn = str(event['Meta']['ActivityArn'])

    while context.get_remaining_time_in_millis() > 90000:
        # We have time to go for another pass
        logger.info('{}ms remaining'.format(context.get_remaining_time_in_millis()))

        if event.get('_Debug', {}).get('_ActivityTaskToken', None) is not None:
            logger.debug('Getting task_token from debug parameters')
            (task_token, task_event) = event['_Debug']['_ActivityTaskToken'], event['_Debug']['_ActivityTaskEvent']
        else:
            logger.info('Polling for task_token')
            response = sfn_client.get_activity_task(
                activityArn=activity_arn,
                workerName=context.log_stream_name[-80:]
            )
            (task_token, task_event) = (response['taskToken'], json.loads(response['input']))
        logger.info('task_token={}, task_event={}'.format(task_token, json.dumps(task_event)))

        if task_token == '':
            # There are no tasks waiting to be scheduled.  Either we're too early (and the Activity hasn't been
            # processed by SFN yet) or we're too late, and it's already been scheduled by another Lambda.
            if get_job_data(execution_id, task_token) is not None:
                # Some other invocation of this Lambda scheduled the jobs.  We're done
                logger.info('Batch jobs already scheduled, nothing to do')
                return
            else:
                # We're too early
                logger.info('No task waiting, sleeping')
                sleep(10)
                continue

        # Otherwise we have scheduling work to do
        scheduled_execution_id = schedule_batch_jobs(task_event, task_token)
        logger.info('Scheduled batch jobs, scheduled_execution_id={}'.format(scheduled_execution_id))

        if scheduled_execution_id == execution_id:
            # We just scheduled our job.  Let's be selfish and not wait for any others
            logger.info('Batch jobs successfully scheduled')
            return
        elif get_job_data(execution_id, task_token) is not None:
            # Some other invocation of this Lambda scheduled the jobs.  We're done
            logger.info('Batch jobs already scheduled, nothing more to do')
            return

        # Otherwise we go round again
        logger.info('Going round for another pass')
        continue

    # When we get here, we have too little time left to be sure of managing a full cycle.  So if our job hasn't come up,
    # we need to fail the invocation so that SFN retries this task and starts a new invocation
    if get_job_data(execution_id, task_token) is not None:
        # Some other execution of this Lambda scheduled the jobs.  We're done
        logger.info('Batch jobs already scheduled')
        return
    else:
        raise Exception("Our activity did not come up for scheduling before the time ran out")

    pass


def handler_schedule_clones(event, context):
    """
    Called when the a Step Function requests that one or more tasks be scheduled as Batch jobs.  This expects an `event`
    input in the following form:
    
    {
        "Meta": {
            "ExecutionArn": <STRING>,
            "ActivityArn": <STRING>
        },
        "Input": <LIST>,
        "Resource": {
            "BatchJobDefinition": <ARN>,
            "BatchJobQueue": <ARN>
        },
    }
    
    The `Resource` ARN must be an AWS Batch Job Definition.  The same job will be scheduled receiving as input each 
    member of the `Input` array
    
    `ExecutionArn` is probably *not* the *actual* ARN of the execution, but any unique value will do.
    
    :param event:
    :param context:
    :return:
    """
    new_event = {'Meta': event.get('Meta', None), 'Input': {}, 'Branches': []}
    resource = event.get('Resource', None)

    i = 0
    for input_data in event.get('Input', []):
        new_event['Input'][i] = input_data
        new_event['Resources'].append({'Resource': resource, 'InputPath': i})
        i += 1

    return handler_schedule(new_event, context)

    pass


def handler_schedule_single(event, context):
    """
    Called when the a Step Function requests that one or more tasks be scheduled as Batch jobs.  This expects an `event`
    input in the following form:
    
    {
        "Meta": {
            "ExecutionArn": <STRING>,
            "ActivityArn": <STRING>
        },
        "Input": <OBJECT>,
        "Resource": {
            "BatchJobDefinition": <ARN>,
            "BatchJobQueue": <ARN>
        },
    }
    
    The `Resource` ARN must be an AWS Batch Job Definition.  The job will be scheduled receiving the `Input` array
    
    `ExecutionArn` is probably *not* the *actual* ARN of the execution, but any unique value will do.
    
    :param event:
    :param context:
    :return:
    """
    return handler_schedule({
        'Meta': event.get('Meta', None),
        'Input': {'only': event.get('Input', None)},
        'Branches': [{'Resource': event.get('Resource', None), 'InputPath': 'only'}],
        '_Debug': event.get('_Debug', {})
    }, context)

    pass


def handler_respond(event, context):
    """
    Called when a Batch Job has completed its work.  This expects an `event`
    input in the following form:
    
    {
        "Meta": {
            "ExecutionArn": <STRING>,
            "BatchJob": {
                "Id": <ID>,
                "Name": <STRING>
            },
            "TaskToken": <STRING>
        },
        "Status": <STATUS>,
        "Error": <STRING>?,
        "Input": <OBJECT>
    }
    :param event: 
    :param context: 
    :return: 
    """
    execution_id = event.get('Meta', {}).get('ExecutionArn', None)
    if execution_id is None:
        raise Exception("Meta.ExecutionArn must be specified")

    batch_job = event.get('Meta', {}).get('BatchJob', None)
    if not isinstance(batch_job, dict):
        raise Exception("Meta.BatchJob must be specified")
    if 'Id' not in batch_job:
        raise Exception("Meta.BatchJob.Id must be specified")
    batch_job = BatchJob(id=batch_job['Id'], name=batch_job['Name'])

    task_token = event.get('Meta', {}).get('TaskToken', None)
    if task_token is None:
        raise Exception("Meta.TaskToken must be specified")

    jobs = get_job_data(execution_id, task_token, batch_job)

    if len(jobs) == 0:
        # There are no records of this job in the database.
        # TODO
        pass

    job = jobs[0]

    if job.task_token is None:
        # No task token was stored
        # TODO
        pass

    status = event.get('Status', None)
    if status == 'SUCCEEDED':
        # This job succeeded
        job.status = 'SUCCEEDED'
        save_job_data([job])

        # Now if we're the last to return, we can signal the SFN Execution to continue
        all_jobs = get_job_data(execution_id, job.task_token)
        if all([job.status == 'SUCCEEDED' for job in all_jobs]):
            sfn_client.send_task_success(
                taskToken=job.task_token,
                output=''  # TODO output aggregation
            )
        else:
            sfn_client.send_task_heartbeat(
                taskToken=job.task_token
            )

    elif status == 'FAILED':
        # If one job failed, the whole parallel execution failed.  Signal the SFN Activity, and it will be re-run and
        # a new set of Batch jobs will be scheduled.
        # TODO currently all the successful parallel jobs will be rescheduled too.  That's wasteful
        sfn_client.send_task_failure(
            taskToken=job.task_token,
            error=event.get('Error', 'BatchJobFailure'),
            cause="Batch job with id '{}' failed".format(job.batch_job.id)
        )

    else:
        # Weird state
        # TODO
        pass


def schedule_batch_jobs(event, task_token):
    """
    Schedule the jobs that have been requested
    :param event: 
    :param task_token: 
    :return: 
    """
    meta = event.get('Meta', None)
    input_data = event.get('Input', None)
    branches = event.get('Branches', None)

    validate_schedule_input(input_data, branches)

    if len(branches) == 1:
        # Only one branch, can return immediately
        meta['TaskToken'] = task_token

    # Submit the jobs!
    jobs = []
    for branch in branches:
        input_datum = get_json_path(input_data, branch['InputPath'])
        job_name = "{}-{}".format(
                meta['ExecutionArn'][:32],
                md5(json.dumps(input_datum, default=json_serial).encode('utf-8')).hexdigest()[:32]
            )

        response = batch_client.submit_job(
            jobName=job_name,
            jobQueue=branch['Resource']['BatchJobQueue'],
            jobDefinition=parse_arn(branch['Resource']['BatchJobDefinition']).resource,
            parameters={
                "Meta": json.dumps(meta, default=json_serial),
                "Input": json.dumps(input_datum, default=json_serial)
            },
            containerOverrides={
                "environment": [{"name": "ENVIRONMENT", "value": meta.get('Environment', 'dev')}]
            }
        )
        logger.info(response)
        jobs.append(BatchRecord(
            execution_id=meta['ExecutionArn'],
            batch_job=BatchJob(id=response['jobId'], name=response['jobName']),
            task_token=task_token,
            status='PENDING'
        ))

    if len(branches) > 1:
        # Submit a blank job that depends on all the others that signals SFN
        # TODO
        pass

    save_job_data(jobs)

    return meta['ExecutionArn']


def get_job_data(execution_id, task_token, job_id: BatchJob = None) -> List[BatchRecord]:
    """
    Get the current state of a job (or all jobs) from the database
    :param execution_id: 
    :param task_token: 
    :type job_id: BatchJob
    :return: 
    """
    # TODO
    return []


def save_job_data(jobs: List[BatchRecord]):
    """
    Save the current state of the jobs to the database
    :return: 
    """
    # TODO
    logger.info(jobs)
    return None


def validate_schedule_input(input_data, branches):
    """
    Validate input
    :param input_data: 
    :param branches: 
    :return: 
    """
    if branches is None:
        raise Exception("Branches must be specified")

    if input_data is None:
        raise Exception("Input must be specified")

    # Validate the InputData for each branch
    for branch in branches:
        path = branch.get('InputPath')
        if not isinstance(path, str) and not isinstance(path, int):
            # Assume the whole path, as per the rest of SFN
            branch['InputPath'] = '$'
        # Check that the path exists
        get_json_path(input_data, branch['InputPath'])

    # Validate the Resource for each branch
    all_batch_job_definition_arns = set()
    all_batch_job_queue_arns = set()

    for branch in branches:
        resource = branch.get('Resource', None)
        if not isinstance(resource, dict):
            raise Exception("Resource must be specified for all branches")

        if 'BatchJobQueue' in resource and 'BatchJobDefinition' in resource:

            job_queue_arn = parse_arn(resource['BatchJobQueue'])
            if job_queue_arn is None or job_queue_arn.service != 'batch' or job_queue_arn.resourcetype != 'job-queue':
                raise Exception("Resource.BatchJobQueue must be a job queue ARN")
            all_batch_job_queue_arns.add(job_queue_arn.resource)

            job_definition_arn = parse_arn(resource['BatchJobDefinition'])
            if job_definition_arn is None or job_definition_arn.service != 'batch' or job_definition_arn.resourcetype != 'job-definition':
                raise Exception("Resource.BatchJobDefinition must be a job definition ARN")
            all_batch_job_definition_arns.add(job_definition_arn.resource.split(':')[0])

    # Check that all the Job Definitions exist
    # TODO pagination if over 100 resources
    res = batch_client.describe_job_definitions(jobDefinitions=list(all_batch_job_definition_arns))
    for job_definition in res['jobDefinitions']:
        if job_definition['status'] != 'ACTIVE':
            raise Exception("Batch Job Definition '{}' is not in 'ACTIVE' status".format(job_definition['jobDefinitionArn']))

    # Check that all the Job Queues exist
    # TODO pagination if over 100 resources
    res = batch_client.describe_job_queues(jobQueues=list(all_batch_job_queue_arns))


def validate_meta(event):
    if event.get('Meta', {}).get('ExecutionArn', None) is None:
        raise Exception("Meta.ExecutionArn must be specified")

    if event.get('Meta', {}).get('ActivityArn') is None:
        raise Exception("Meta.ActivityArn must be specified")


def get_json_path(source, path):
    if path == '$':
        return source

    parts = path.split('.')
    if parts[0] != '$':
        raise Exception("Path must be based on '$'")
    if len(parts) > 2:
        raise Exception("Subpaths not currently supported")
    if parts[1] not in source:
        raise Exception("Path '{}' not found in Input".format(path))
    return source[parts[1]]


def parse_arn(arn: str) -> Arn:
    """
    Parse an ARN into its constituent components.  Based on schema at 
    http://docs.aws.amazon.com/general/latest/gr/aws-arns-and-namespaces.html
    :param arn: string
    :return: dict
    """
    match = re.match('^arn:aws:(?P<service>[\w\-]+):(?P<region>[a-zA-Z]+-[a-zA-Z]+-\d+|):(?P<account>\d*):(?P<resourcetype>.+?)(?:[:\/](?P<resource>.*))?$', arn)
    if not match:
        logger.warning("Failed to parse '{}' as an ARN".format(arn))
        return None
    else:
        logger.info(match.groupdict())
        return Arn(**{**match.groupdict(), **{'partition': 'aws'}})


def json_serial(obj):
    """
    JSON serializer for objects not serializable by default json code
    """
    from datetime import datetime
    if isinstance(obj, datetime):
        serial = obj.isoformat()
        return serial
    raise TypeError("Type not serializable")