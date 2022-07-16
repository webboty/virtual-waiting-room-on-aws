# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
This module is the get_public_key API handler.
It retrieves the public key generated by a custom resource stored in Secrets Manager.
"""

import boto3
import os
import redis
from botocore import config
from time import time
from boto3.dynamodb.conditions import Key, Attr
from counters import MAX_QUEUE_POSITION_EXPIRED, SERVING_COUNTER

SECRET_NAME_PREFIX = os.environ["STACK_NAME"]
SOLUTION_ID = os.environ['SOLUTION_ID']
EVENT_ID = os.environ["EVENT_ID"]
REDIS_HOST = os.environ["REDIS_HOST"]
REDIS_PORT = os.environ["REDIS_PORT"]
QUEUE_POSITION_ISSUEDAT_TABLE = os.environ["QUEUE_POSITION_ISSUEDAT_TABLE"]
QUEUE_POSITION_EXPIRY_PERIOD = os.environ["QUEUE_POSITION_EXPIRY_PERIOD"]
SERVING_COUNTER_ISSUEDAT_TABLE = os.environ["SERVING_COUNTER_ISSUEDAT_TABLE"]

user_agent_extra = {"user_agent_extra": SOLUTION_ID}
user_config = config.Config(**user_agent_extra)
boto_session = boto3.session.Session()
region = boto_session.region_name
ddb_resource = boto3.resource('dynamodb', endpoint_url=f'https://dynamodb.{region}.amazonaws.com', config=user_config)
ddb_table_queue_position_issued_at = ddb_resource.Table(QUEUE_POSITION_ISSUEDAT_TABLE)
ddb_table_serving_counter_issued_at = ddb_resource.Table(SERVING_COUNTER_ISSUEDAT_TABLE)
secrets_client = boto3.client('secretsmanager', config=user_config, endpoint_url=f'https://secretsmanager.{region}.amazonaws.com')
response = secrets_client.get_secret_value(SecretId=f"{SECRET_NAME_PREFIX}/redis-auth")
redis_auth = response.get("SecretString")
rc = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, ssl=True, decode_responses=True, password=redis_auth)

def lambda_handler(event, context):
    """
    This function is the entry handler for Lambda.
    """
    # scan for all items that are not obsolete 
    print(event)

    max_queue_position_expired = int(rc.get(MAX_QUEUE_POSITION_EXPIRED))
    serving_counter = int(rc.get(SERVING_COUNTER))
    
    # get items between the last max queue position expired value and serving counter 
    kce = Key('event_id').eq(EVENT_ID) & Key('queue_position').between(max_queue_position_expired, serving_counter)
    fexp = Attr('expired').eq(0)
    response = ddb_table_queue_position_issued_at.query(
        KeyConditionExpression=kce,
        ScanIndexForward=False,
        FilterExpression=fexp
    )
    queue_position_items = response['Items']

    if not queue_position_items:
        print('No queue positions avaialable to be marked for expiry')
        return 
    
    # get last queue position and the issue time
    last_queue_position = int(queue_position_items[-1]['queue_position'])
    print(f'last queue position: {last_queue_position}')

    kce = Key('event_id') & Key('serving_counter').gte(last_queue_position)
    response = ddb_table_serving_counter_issued_at.query(
        KeyConditionExpression=kce,
        ScanIndexForward=True,
    )
    serving_counter_items = response['Items']

    if not serving_counter_items:
        print('No serving counter items avaialable for checking')
        return

    # get issue time of first value of the list 
    serving_counter_issue_time = int(serving_counter_items[0]['issue_time'])

    # mark expired items with expired issue time
    increment_by = 0
    current_time = int(time)
    for queue_item in queue_position_items:
        # if int(queue_item['issue_time']) - serving_counter_issue_time > int(QUEUE_POSITION_EXPIRY_PERIOD): # check this logic 
        if current_time - serving_counter_issue_time > int(QUEUE_POSITION_EXPIRY_PERIOD): # check this logic 
            ddb_table_queue_position_issued_at.update_item(
            Key={
                'event_id': queue_item['event_id'],
                'queue_position': queue_item['queue_position']
            },  
            UpdateExpression='SET expired = :val1',
            ExpressionAttributeValues={':val1': 1}
        )
        increment_by += 1
    
    max_queue_position_expired = rc.incrby(MAX_QUEUE_POSITION_EXPIRED, increment_by)
    print(f'max queue_position incremented by :{increment_by} to {max_queue_position_expired}')

