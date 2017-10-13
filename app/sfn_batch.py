#!/usr/bin/env python3
# Copyright 2017 Melon Software Ltd (UK), all rights reserved
#
import logging
from collections import namedtuple

import boto3
import copy
import json
import os
import re
import sys
import time
import uuid
from hashlib import md5

logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
logger = logging.getLogger()
logger.setLevel(logging.INFO)

sfn_client = boto3.client('stepfunctions')
sqs_client = boto3.client('sqs')
batch_client = boto3.client('batch')

Arn = namedtuple('Arn', ['partition', 'service', 'region', 'account', 'resourcetype', 'resource'])
BatchJob = namedtuple('BatchJob', ['id', 'name'])
BatchRecord = namedtuple('BatchRecord', ['execution_id', 'batch_job', 'task_token', 'status'])


def sfn_runner(activity_arn, sqs_arn, worker_name):
    """
    An eternal loop for polling for SFN activities and scheduling the corresponding batch jobs
    """
    while True:
        try:
            logger.info('Polling for task_token')
            response = sfn_client.get_activity_task(
                activityArn=activity_arn,
                workerName=worker_name
            )
            (task_token, task_event) = (response.get('taskToken', ''), json.loads(response.get('input', '""')))
            logger.debug(response)
            logger.info('task_token={}'.format(task_token))
            logger.info(json.dumps(task_event))

            if task_token == '':
                # There are no tasks waiting to be scheduled.
                logger.info('No task waiting')
                continue

            # Sending a heartbeat also confirms that it hasn't already timed out
            send_sfn_heartbeat(task_token)

            if 'LinearityGroup' in task_event and task_event['LinearityGroup']:
                deduplication_id = str(uuid.uuid4())
                # Need to push this through an SQS queue to enforce one-at-a-time processing to avoid race conditions
                sqs_client.send_message(
                    QueueUrl=sqs_arn,
                    MessageBody=json.dumps({'TaskToken': task_token, 'TaskEvent': task_event}),
                    MessageDeduplicationId=deduplication_id,
                    MessageGroupId=task_event['LinearityGroup']
                )
                logger.info('Sent batch job to SQS for linearity enforcement, with deduplication_id={}'.format(deduplication_id))
            else:
                scheduled_execution_id = schedule_batch_jobs(task_event, task_token)
                logger.info('Scheduled batch jobs, scheduled_execution_id={}'.format(scheduled_execution_id))

        except BaseException as e:
            # On any error, print and continue
            logger.error("An error occurred:")
            logger.error(repr(e))
            time.sleep(10)


def sqs_runner(sqs_arn):
    """
    An eternal loop for polling an SQS queue and scheduling the corresponding batch jobs
    """
    while True:
        try:
            rraid = str(uuid.uuid4())
            logger.info('Polling for task_token with ReceiveRequestAttemptId={}'.format(rraid))
            response = sqs_client.receive_message(
                QueueUrl=sqs_arn,
                AttributeNames=['MessageGroupId'],
                WaitTimeSeconds=20,
                MaxNumberOfMessages=1,
                ReceiveRequestAttemptId=rraid
            )
            if len(response.get('Messages', [])) == 0:
                continue

            task_body = json.loads(response['Messages'][0]['Body'])
            (task_token, task_event) = (task_body['TaskToken'], task_body['TaskEvent'])

            logger.debug(response)
            logger.info('task_token={}'.format(task_token))
            logger.info(json.dumps(task_event))

            # Sending a heartbeat also confirms that it hasn't already timed out
            send_sfn_heartbeat(task_token)

            scheduled_execution_id = schedule_batch_jobs(task_event, task_token)
            logger.info('Scheduled batch jobs from SQS, scheduled_execution_id={}'.format(scheduled_execution_id))

        except BaseException as e:
            # On any error, print and continue
            logger.error("An error occurred:")
            logger.error(repr(e))
            time.sleep(10)


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

    # Always include the task token to use in case of failure
    meta['TaskTokenFailure'] = task_token
    meta['Profiling'] = {
        'ScheduleTime': time.time(),
    }

    if len(branches) == 1:
        # Only one branch, can return immediately
        meta['TaskToken'] = task_token

    execution_name = meta.get('ExecutionName', md5(meta['ExecutionArn'].encode('utf-8')).hexdigest())[:23]

    # Submit the jobs!
    jobs = []
    for branch in branches:
        input_datum = get_json_path(input_data, branch['InputPath'])
        branch_name = md5(json.dumps(input_datum, default=json_serial).encode('utf-8')).hexdigest()[:8]
        job_name = "{}-{}-{}".format(
            execution_name,
            branch['Resource']['BatchJob'],
            branch_name
        )

        branch_meta = copy.deepcopy(meta)
        branch_meta['Profiling']['BranchScheduleTime'] = time.time()

        response = batch_client.submit_job(
            jobName=job_name,
            jobQueue=branch['Resource']['BatchJobQueue'],
            jobDefinition=parse_arn(branch['Resource']['BatchJobDefinition']).resource,
            parameters={
                "Job": branch['Resource']['BatchJob'],
                "Meta": json.dumps(branch_meta, default=json_serial),
                "Input": json.dumps(input_datum, default=json_serial)
            },
            containerOverrides={
                "environment": [
                    {"name": "ENVIRONMENT", "value": meta.get('Environment', 'dev')},
                    {"name": "AWS_DEFAULT_REGION", "value": os.environ['AWS_DEFAULT_REGION']},
                ],
                "memory": branch['Resource'].get('Memory', 256),
                "vcpus": branch['Resource'].get('Vcpus', 2),
            }
        )
        logger.debug(response)
        jobs.append(BatchRecord(
            execution_id=meta['ExecutionArn'],
            batch_job=BatchJob(id=response['jobId'], name=response['jobName']),
            task_token=task_token,
            status='PENDING'
        ))

    if len(branches) > 1:
        # Submit a blank job that depends on all the others that signals SFN
        meta['TaskToken'] = task_token
        response = batch_client.submit_job(
            jobName="{}-{}".format(
                execution_name,
                'noop'
            ),
            jobQueue=branches[0]['Resource']['BatchJobQueue'],
            jobDefinition=parse_arn(meta['NoopJobDefinition']).resource,
            parameters={
                "Job": 'noop',
                "Meta": json.dumps(meta, default=json_serial),
                "Input": "{}"
            },
            dependsOn=[{'jobId': job.batch_job.id} for job in jobs],
            containerOverrides={
                "environment": [
                    {"name": "ENVIRONMENT", "value": meta.get('Environment', 'dev')},
                    {"name": "AWS_DEFAULT_REGION", "value": os.environ['AWS_DEFAULT_REGION']},
                ],
                "memory": 64,
                "vcpus": 1,
            }
        )
        logger.debug(response)

    return meta['ExecutionArn']


def send_sfn_heartbeat(task_token):
    if task_token is not None:
        sfn_client.send_task_heartbeat(
            taskToken=task_token,
        )


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
            raise Exception(
                "Batch Job Definition '{}' is not in 'ACTIVE' status".format(job_definition['jobDefinitionArn']))

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
    parts = re.sub('\[([^\]]+)\]', '.\\1.', path).strip('.').split('.')
    if parts[0] != '$':
        raise Exception("Path must be based on '$'")
    if len(parts) > 2:
        raise Exception("Subpaths not currently supported")
    if isinstance(source, dict):
        if parts[1] not in source:
            raise Exception("Path '{}' not found in Input".format(path))
        return source[parts[1]]
    if isinstance(source, list):
        if len(source) < int(parts[1]):
            raise Exception("List index out of range")
        return source[int(parts[1])]


def parse_arn(arn: str) -> Arn:
    """
    Parse an ARN into its constituent components.  Based on schema at
    http://docs.aws.amazon.com/general/latest/gr/aws-arns-and-namespaces.html
    :param arn: string
    :return: dict
    """
    match = re.match(
        '^arn:aws:(?P<service>[\w\-]+):(?P<region>[a-zA-Z]+-[a-zA-Z]+-\d+|):(?P<account>\d*):(?P<resourcetype>.+?)(?:[:\/](?P<resource>.*))?$',
        arn)
    if not match:
        logger.warning("Failed to parse '{}' as an ARN".format(arn))
        return None
    else:
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


if __name__ == '__main__':
    arg_sqs_arn = os.environ.get('SQS_QUEUE_NAME', None)
    if not arg_sqs_arn:
        raise Exception("No SQS ARN given")

    arg_worker_name = os.environ.get('WORKER_NAME', None)

    source = os.environ.get('SOURCE', 'SFN')

    if source == 'SFN':
        arg_activity_arn = os.environ.get('ACTIVITY_ARN', None)
        if not arg_activity_arn:
            raise Exception("No activity ARN given")
        sfn_runner(arg_activity_arn, arg_sqs_arn, arg_worker_name)

    elif source == 'SQS':
        sqs_runner(arg_sqs_arn)

    else:
        raise Exception("Unknown source {}".format(source))
