from asyncio import SubprocessProtocol
from email.message import EmailMessage
from inspect import Parameter
from lib2to3.pgen2.token import STRING
from msilib.schema import Environment
import aws_cdk as cdk
from constructs import Construct
from aws_cdk import (aws_lambda as lambda_,
                     aws_sns as sns,
                     aws_sns_subscriptions as subscriptions,
                     aws_dynamodb as dynamodb,
                     aws_cloudwatch as cloudwatch,
                     aws_events as events,
                     aws_events_targets as targets,
                     aws_ssm as ssm,
                     aws_secretsmanager as secretsmanager
                     )

class HashtagCdkStack(cdk.Stack):

    def __init__(self, scope: Construct, id: str):
        super().__init__(scope, id)

        # Creating parameter in Systems Manager to store date last notification
        # was sent.  Once notification is sent, don't want to spam.
        param = ssm.StringParameter(self, "parameter",
            parameter_name="/hashtag_query/last_notification",
            string_value="0",
            description="Used to track the time the last notificaiton was sent",
            type=ssm.ParameterType.STRING,
            tier=ssm.ParameterTier.STANDARD,
            allowed_pattern=".*"
            )

        # The SNS topic is used to publish notificaitons when needed
        topic = sns.Topic(self, "notification",
            topic_name="outage_notification",
            display_name="Topic used for publishing outage notifications"
            )

        # Email subscription created for the topic (more people could be added here)
        topic.add_subscription(
            subscriptions.EmailSubscription("akasper@outlook.com")
            )

        # SMS subscription for topic - requires originating Pinpoint phone number.  Pinpoint
        # not available in Ohio so this is provisioned to us-west-2. By default, a new account
        # is in Sandbox until it has been used and Support case opened to move out. When in the
        # sandbox, we must verify phone numbers we're sending to
        topic.add_subscription(
            subscriptions.SmsSubscription("+16144049515")
            )

        # Secret previously added to Secrets Manager to store Twitter API key. This will define
        # where to find the key and later access can be given
        secret = secretsmanager.Secret.from_secret_attributes(self, "ImportedSecret",
            secret_complete_arn="arn:aws:secretsmanager:us-west-2:743304922740:secret:prod/hashtag_cdk/twitter_api-8peEXp"
            )
 
        # The DynamoDB table is used to store twitter results.  We are only tracking tweet ID
        # and the timestamp it was created.  Since the purpose of the app is for real time 
        # notification, we only need a few hours or days of data.  This table is destroyed when
        # the stack is destroyed.  The data is automatcially regenerated within a few mins of 
        # deployment.
        table = dynamodb.Table(self, "twitter_hashtag_table",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            partition_key=dynamodb.Attribute(name="tweetID", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PROVISIONED
            )

        # This Lambda function calls the Twitter API and pushes results to DyanmoDB
        TQhandler = lambda_.Function(self, "query_handler",
            runtime=lambda_.Runtime.PYTHON_3_8,
            code=lambda_.Code.from_asset("resources"),
            handler="tweet_query.lambda_handler",
            timeout=cdk.Duration.seconds(60),
            environment=dict(TABLE=table.table_name,
                KEY_ARN=secret.secret_arn)
            )

        # Setting up a trigger to start the function based on a cron schedule
        rule = events.Rule(self, "query_schedule",
            schedule=events.Schedule.cron(minute="*/5")
            )
        rule.add_target(targets.LambdaFunction(TQhandler))

        # If too many results are returned from Twitter, we'll time out.  We got what we
        # needed but we'll record a cloudwatch alarm so we know it happened.
        if TQhandler.timeout:
            cloudwatch.Alarm(self, "query_alarm",
                metric=TQhandler.metric_duration(statistic="Maximum"),
                evaluation_periods=1,
                datapoints_to_alarm=1,
                threshold=TQhandler.timeout.to_milliseconds(),
                treat_missing_data=cloudwatch.TreatMissingData.IGNORE,
                alarm_name="Query Function Timeout"
            )

        # This Lambda function queries DynamoDB, counts the results in hourly bins and 
        # performs a standard deviation.  If standard deviation is off the charts, it calls
        # the SNS topic 
        TAhandler = lambda_.Function(self, "analyzer_handler",
            runtime=lambda_.Runtime.PYTHON_3_8,
            code=lambda_.Code.from_asset("resources"),
            handler="tweet_analyzer.lambda_handler",
            timeout=cdk.Duration.seconds(60),
            environment=dict(TABLE=table.table_name,
                LASTSENT=param.parameter_name,
                SNSTOPIC=topic.topic_arn)
            )

        # Setting up a cron schedule to trigger the analyzer
        rule = events.Rule(self, "analyzer_schedule",
            schedule=events.Schedule.cron(minute="*/5")
            )
        rule.add_target(targets.LambdaFunction(TAhandler))

        # Given the analyzer only queries from DynamoDB and performs a little math, it's
        # unlikely it will timeout but we'll track it just in case
        if TAhandler.timeout:
            cloudwatch.Alarm(self, "analyzer_alarm",
                metric=TAhandler.metric_duration(statistic="Maximum"),
                evaluation_periods=1,
                datapoints_to_alarm=1,
                threshold=TAhandler.timeout.to_milliseconds(),
                treat_missing_data=cloudwatch.TreatMissingData.IGNORE,
                alarm_name="Analyzer Function Timeout"
            )

        # Grant Analyzer Lambda function access to the parameter store, SNS topic and DynamoDB
        param.grant_read(TAhandler)
        param.grant_write(TAhandler)
        topic.grant_publish(TAhandler)
        table.grant_read_data(TAhandler)

        # Grant Query Lambda function access to Secrets Manager and Dynamodb
        table.grant_write_data(TQhandler)
        secret.grant_read(TQhandler)
