import boto3
import os
import datetime as dt
import json
import urllib3
import dateutil.parser as parser
from urllib.parse import urlencode

def lambda_handler(event, context):
    secrets_client = boto3.client('secretsmanager')
    dynamodb = boto3.resource('dynamodb')
    sns = boto3.client('sns')
    
    # Nunmber of hours back to query from Twitter API 
    QUERYTIME = 1
    # Hashtag to search for
    HASHTAG = "awsoutage"
    # Number of results to return, 100 max.  Need to use next_token to get more
    MAX_RESULTS = 100

    dtNow = dt.datetime.now(dt.timezone.utc) 
    start_time = (dtNow - dt.timedelta(hours=QUERYTIME)).isoformat()

    # Grab the SNS Topic name from the Lambda's environment variable
    sns_arn = os.environ['SNSTOPIC']

    # Grab DynamoDB table name from Lambda environment variable
    mytable = os.environ['TABLE']
    table = dynamodb.Table(mytable)

    # print('## Environment Variables: ', os.environ)
    # print('## API Key ARN: ', os.environ['KEY_ARN'])

    # Grab Twitter API Header token from Secrets Manager ARN in Lambda environment variable
    secret_json = json.loads(secrets_client.get_secret_value(SecretId=os.environ['KEY_ARN']).get('SecretString'))
    auth_token = secret_json['prod/hashtag_cdk/twitter_api']

    next_token=""
    again=True
    while again:
        url = "https://api.twitter.com/2/tweets/search/recent?"
        querystring = {"query":HASHTAG,
                    "max_results":MAX_RESULTS,
                    "tweet.fields":"created_at",
                    "user.fields":"created_at"
                    }

        # The first pass needs a start_time to lookup.  Each time we query the next 100
        # results, we pass the next_token Twitter gave us instead of the start_time
        if next_token:
            querystring["next_token"] = next_token
        else:
            querystring["start_time"] = start_time

        headers = {'Authorization': 'Bearer '+ auth_token}
        http = urllib3.PoolManager()
        url = url + urlencode(querystring)
        print(url)

        res = http.request(method='GET',
                        url=url,
                        headers=headers)
        if res.status != 200:
            print("Exited with status: ", urllib3.exceptions.HTTPError(res.status))
            print(res.data)
            return
        tweets_data = json.loads(res.data)
        
        count = tweets_data['meta']['result_count']
        if count == 0:
            print(tweets_data)
            print("No results returned")
 
            print("Attempting to publish to SNS topic: " + sns_arn)
            message =  "Completed run of Tweet Query Lambda Handler with the following results"
            message += str(tweets_data)
            # response = sns.publish (
            # TargetArn = sns_arn,
            # Message = json.dumps({'default': message}), MessageStructure = 'json')

            return
        else:
            print("Twitter API returned " + str(count) + " results.")

        # Since we know there were results returned, we now record them in DynamoDB.  It's
        # expected that we will query some of the same tweets each time we run the Lambda
        # fucntion so we use the Tweet ID as the unique key so we only store it once.
        for tweet in tweets_data["data"]:
            id = tweet['id']
            time = int(dt.datetime.timestamp(parser.parse(tweet['created_at'])))
            # print('{%s: %d}' % (id, time))
            
            table.put_item(
                Item={
                    'tweetID': tweet['id'],
                    'created_at': time
                    }
            )

        # If we made it this far, there are are more results to go get so we grab the next_token
        if "next_token" in tweets_data["meta"]:
            next_token = tweets_data["meta"]["next_token"]
            print(next_token)
        else:
            again = False
