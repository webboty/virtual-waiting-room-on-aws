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
from boto3.dynamodb.conditions import Key
from counters import MAX_QUEUE_POSITION_EXPIRED, QUEUE_COUNTER, SERVING_COUNTER

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
    print(event)
    current_time = int(time())

    max_queue_position_expired = int(rc.get(MAX_QUEUE_POSITION_EXPIRED))
    current_serving_counter_position = int(rc.get(SERVING_COUNTER))
    queue_counter = int(rc.get(QUEUE_COUNTER))
    print(f'Queue counter: {queue_counter}. Max position expired: {max_queue_position_expired}. Serving counter: {current_serving_counter_position}')

    # find items in the serving counter table that are greater than the max queue position expired
    response = ddb_table_serving_counter_issued_at.query(
        KeyConditionExpression=Key('event_id').eq(EVENT_ID) & Key('serving_counter').gt(max_queue_position_expired),
    )
    serving_counter_items = response['Items']

    if not serving_counter_items:
        print('No serving counter items avaialable for checking')
        return

    # set previous serving counter to max queue position expired
    previous_serving_counter_position = max_queue_position_expired

    for serving_counter_item in serving_counter_items:

        serving_counter_item_position = int(serving_counter_item['serving_counter'])
        serving_counter_issue_time = int(serving_counter_item['issue_time'])
        
        # query queue position table for corresponding serving counter item position
        response = ddb_table_queue_position_issued_at.query(
            KeyConditionExpression=Key('event_id').eq(EVENT_ID) & Key('queue_position').eq(serving_counter_item_position)
        )
        queue_position_items = response['Items']
        
        if not queue_position_items:
            break
        
        queue_item_issue_time = int(queue_position_items[0]['issue_time'])
        time_in_queue = max(queue_item_issue_time, serving_counter_issue_time)

        # if time in queue has not exceeded expiry period
        if current_time - time_in_queue < int(QUEUE_POSITION_EXPIRY_PERIOD):
            break
                
        max_queue_position_expired = rc.set(MAX_QUEUE_POSITION_EXPIRED, serving_counter_item_position)
        print(f'Max queue expiry position set to: {max_queue_position_expired}')        

        # increment the serving counter (Current - Previous) - (Positions served)
        increment_by = (serving_counter_item_position - previous_serving_counter_position) - int(serving_counter_item['queue_positions_served'])
        cur_serving = rc.incrby(SERVING_COUNTER, int(increment_by))
        item = {
            'event_id': EVENT_ID,
            'serving_counter': cur_serving,
            'issue_time': int(time()),
            'queue_positions_served': 0
        }
        ddb_table_serving_counter_issued_at.put_item(Item=item)
        print(item)
        print(f'Serving counter incremented by {increment_by}. Current value: {cur_serving}')

        # set prevous serving counter position to item serving counter position
        previous_serving_counter_position = serving_counter_item_position