import boto3
import os
import json
import time
from datetime import datetime, timedelta
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError
import statistics

# This fuction is used to take a list and count the number of times a number
# shows up in-between a min and max.  Each Tweet has a create_at date.  Those
# dates are converted to UNIX EPOCH time which makes it an integer representing
# the number of seconds from Jan 1, 1970.  The end result is a series of bins
# containing the count of tweets in each bin.
def count_range_in_list(list, min, max):
    counter=0
    for item in list: 
        if min <= item <= max: 
            counter += 1
    return counter
	
def lambda_handler(event, context):
    sns = boto3.client('sns')
    ssm = boto3.client('ssm')
    dynamodb = boto3.resource('dynamodb')

    # This is the number of hours of data to query from DynamoDB.  We're looking for a 
    # huge spike in tweets so it's very much a real-time notification.
    HOURS_AGO = 6

    # Grab the DynamoDB table name from the Lambda's environment variable
    table = dynamodb.Table(os.environ['TABLE'])

    # Grab the SNS Topic name from the Lambda's environment variable
    sns_arn = os.environ['SNSTOPIC']
    
    end_time = int(datetime.timestamp(datetime.now()))
    start_time = int(datetime.timestamp(datetime.now() - timedelta(hours=HOURS_AGO)))

    # Query DynamoDB for records within a time frame
    Fe = Key('created_at').between(start_time, end_time);
    response = table.scan(
                  FilterExpression=Fe
              )
    print("DynamoDB table query included",response['Count'], "items.")

    # DynamoDB returns a list of dictionarys.  Each dictionary reprsents a Tweet 
    # consisting of a Tweet ID and a create_at timestamp.  It's not easy working 
    # with a list of dictionarys so this next line converts the object to a list
    # of timestamps
    mylist = [l['created_at'] for l in response['Items']]
    print("List of dates included ", len(mylist), "items.")

    distribution = []
    min = start_time
    print("Start time:", datetime.fromtimestamp(int(start_time)))
    print("  End time:", datetime.fromtimestamp(int(end_time)))
    
    again = True
    i = 3600                # number of seconds in an hour
    min = start_time
    max = start_time + i

    # We are stepping through this while loop in timed increments.  For exmaple,
    # we count all tweets in the first hour, then again in the second hour and 
    # so on.  When the loop is completed, we end up with a list of tweet counts
    # over some distribution of time.  
    while again:
        # print("Counting tweets between", min, "and", max)
        counter = count_range_in_list(mylist, min, max)
        # print(counter)
        distribution.append(counter)
        min = max
        max += i
        if max > end_time: again = False
    print("Distribution:",distribution)

    # We use the distribution of tweets per period to calculate a standard deviation.
    stdev = statistics.stdev(distribution)
    print("Standard Deviation: ", "{:.2f}".format(stdev))

    # Under normal operating sitinations, the standard deviation for a particular
    # tweet can vary wildly.  In the case of outages, it usually falls between 0 and 30
    # but it can go higher.  In a real massive outage situation, it would probably jump
    # up over 500.
    if stdev < 100: 
      print("Standard Deviation within boundaries. Exiting.")
      return None

    # The function runs a recurring basis and once there is a spike, it could take hours
    # before it stablizes and the standard deviation drops again.  We don't want to 
    # spam our audience every couple minutes so we keep track of when we sent the last
    # notification by storing the timestamp in the SSM Parameter Store.  The location
    # of the variable is passed from the CDK deply to the Lambda's environment.
    last_sent_parameter_name = os.environ['LASTSENT']
    parameter = ssm.get_parameter(Name=last_sent_parameter_name, WithDecryption=True)
    last_sent = int(parameter ['Parameter']['Value'])

    five_hours_ago = int(datetime.timestamp(datetime.now() - timedelta(hours=5)))
    now = int(datetime.now().timestamp())

    print("Last notification sent at:", datetime.fromtimestamp(last_sent))
    if last_sent > five_hours_ago:
        print("Too soon to send another, exiting.")
        return None

    print("Updating SSM Parameter with current send time")
    result = ssm.put_parameter(
        Name=last_sent_parameter_name,
        Value=str(now),
        Overwrite=True
    )

    # Notfications are publish to the SNS topic and subscritions are added to the
    # topic.  This way we don't need to worry about how needs to be notified from
    # the Lambda function.
    print("Attempting to publish to SNS topic: " + sns_arn)
    message =  "Elevated levels of activity on Twitter."
    message += "Distribution over past 6 hours: " + str(distribution)
    message += "Standard Deviation: " + str(stdev)
    response = sns.publish (
      TargetArn = sns_arn,
      Message = json.dumps({'default': message}),
      MessageStructure = 'json'
    )

