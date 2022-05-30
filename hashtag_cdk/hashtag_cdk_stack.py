import aws_cdk as cdk
import json
from dotmap import DotMap
from constructs import Construct
from aws_cdk.custom_resources import (
    AwsCustomResource,
    AwsCustomResourcePolicy,
    PhysicalResourceId,
    AwsSdkCall
)
from aws_cdk import (aws_lambda as lambda_,
                     aws_sns as sns,
                     aws_sns_subscriptions as subscriptions,
                     aws_dynamodb as dynamodb,
                     aws_cloudwatch as cloudwatch,
                     aws_events as events,
                     aws_events_targets as targets,
                     aws_ssm as ssm,
                     aws_secretsmanager as secretsmanager,
                     aws_apigateway as apigateway,
                     aws_codecommit as codecommit,
                     aws_amplify_alpha as amplify,
                     aws_route53 as route53,
                     aws_certificatemanager as acm,
                     triggers as triggers
                     )

def load_config(ConfigFile):
    with open(ConfigFile) as json_config_file:
        config_dict = json.load(json_config_file)
    config = DotMap(config_dict)
    return config

class HashtagCdkEastStack(cdk.Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        config = load_config('./config.json')

        # Creating parameter in Systems Manager to store date last notification
        # was sent.  Once notification is sent, don't want to spam.
        param = ssm.StringParameter(self, "LastNotification",
            parameter_name="/hashtag_query/last_notification",
            string_value="0",
            description="Used to track the time the last notificaiton was sent",
            type=ssm.ParameterType.STRING,
            tier=ssm.ParameterTier.STANDARD,
            allowed_pattern=".*"
            )
        cdk.CfnOutput(self, "LastNotificationARN", value=param.parameter_arn)
        cdk.CfnOutput(self, "LastNotificationName", value=param.parameter_name)

        # The SNS topic is used to publish notificaitons when needed
        topic = sns.Topic(self, "SNSTopic",
            topic_name="outage_notifier_topic",
            display_name="Topic used for publishing outage notifications"
            )
        cdk.CfnOutput(self, "SNSTopicARN", value=topic.topic_arn)
        cdk.CfnOutput(self, "SNSTopicName", value=topic.topic_name)

        # Email subscription created for the topic (more people could be added here)
        topic.add_subscription(
            subscriptions.EmailSubscription(config.SNSTopic.EmailSubscription)
            )

        # SMS subscription for topic - requires originating Pinpoint phone number.  Pinpoint
        # not available in Ohio so this is provisioned to us-west-2. By default, a new account
        # is in Sandbox until it has been used and Support case opened to move out. When in the
        # sandbox, we must verify phone numbers we're sending to        
        topic.add_subscription(
            subscriptions.SmsSubscription(config.SNSTopic.SubscriptionPhonenumber)
            )

        # Secret previously added to Secrets Manager to store Twitter API key. This will define
        # where to find the key and later access can be given
        secret = secretsmanager.Secret.from_secret_attributes(self, "TwitterAPISecret",
            secret_complete_arn=config.TwitterAPISecret.APIkeyARN
            )
        cdk.CfnOutput(self, "TwitterAPISecretARN", value=secret.secret_arn)
        cdk.CfnOutput(self, "TwitterAPISecretName", value=secret.secret_name)
 
        # The DynamoDB table is used to store twitter results.  We are only tracking tweet ID
        # and the timestamp it was created.  Since the purpose of the app is for real time 
        # notification, we only need a few hours or days of data.  This table is destroyed when
        # the stack is destroyed.  The data is automatcially regenerated within a few mins of 
        # deployment.
        table = dynamodb.Table(self, "GlobalTable",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            partition_key=dynamodb.Attribute(name="tweetID", type=dynamodb.AttributeType.STRING),
            replication_regions=['us-west-2'],
            billing_mode=dynamodb.BillingMode.PROVISIONED
            )
        cdk.CfnOutput(self, "GlobalTableARN", value=table.table_arn)
        cdk.CfnOutput(self, "GlobalTableName", value=table.table_name)

        table.auto_scale_write_capacity(
            min_capacity=1,
            max_capacity=10
        ).scale_on_utilization(target_utilization_percent=75)

        # When running multiple CloudFormation stacks within the same region, you're able to 
        # share references across stacks using CloudFormation Outputs.  However, outputs 
        # cannot be used for cross region references.  The easiest way is to write the data to 
        # the Systems Manager Parameter Store 
        param = ssm.StringParameter(self, "GlobalTableParameter",
            parameter_name="/hashtag_query/global_table_name",
            string_value=table.table_name,
            description="Global DynamoDB table shared across regions",
            type=ssm.ParameterType.STRING,
            tier=ssm.ParameterTier.STANDARD,
            allowed_pattern=".*"
            )

        # This Lambda function calls the Twitter API and pushes results to DyanmoDB
        query_function = lambda_.Function(self, "QueryFunction",
            runtime=lambda_.Runtime.PYTHON_3_9,
            code=lambda_.Code.from_asset("resources"),
            handler="tweet_query.lambda_handler",
            timeout=cdk.Duration.seconds(int(config.query_function.TimeoutDuration)),
            environment=dict(TABLE=table.table_name,
                KEY_ARN=secret.secret_arn,
                SNSTOPIC=topic.topic_arn)
            )
        cdk.CfnOutput(self, "QueryFunctionARN", value=query_function.function_arn)
        cdk.CfnOutput(self, "QueryFunctionName", value=query_function.function_name)

        # Setting up a trigger to start the function based on a cron schedule
        rule = events.Rule(self, "QuerySchedule",
            schedule=events.Schedule.cron(minute="*/5")
            )
        rule.add_target(targets.LambdaFunction(query_function))

        # If too many results are returned from Twitter, we'll time out.  We got what we
        # needed but we'll record a cloudwatch alarm so we know it happened.
        if query_function.timeout:
            cloudwatch.Alarm(self, "QueryAlarm",
                metric=query_function.metric_duration(statistic="Maximum"),
                evaluation_periods=1,
                datapoints_to_alarm=1,
                threshold=query_function.timeout.to_milliseconds(),
                treat_missing_data=cloudwatch.TreatMissingData.IGNORE,
                alarm_name="Query Function Timeout"
            )

        # This Lambda function queries DynamoDB, counts the results in hourly bins and 
        # performs a standard deviation.  If standard deviation is off the charts, it calls
        # the SNS topic 
        analyzer_function = lambda_.Function(self, "AnalyzerFunction",
            runtime=lambda_.Runtime.PYTHON_3_9,
            code=lambda_.Code.from_asset("resources"),
            handler="tweet_analyzer.lambda_handler",
            timeout=cdk.Duration.seconds(int(config.analyzer_function.TimeoutDuration)),
            environment=dict(TABLE=table.table_name,
                LASTSENT=param.parameter_name,
                SNSTOPIC=topic.topic_arn)
            )
        cdk.CfnOutput(self, "AnalyzerFunctionARN", value=analyzer_function.function_arn)
        cdk.CfnOutput(self, "AnalyzerFunctionName", value=analyzer_function.function_name)

        # Setting up a cron schedule to trigger the analyzer
        rule = events.Rule(self, "AnalyzerSchedule",
            schedule=events.Schedule.cron(minute="*/5")
            )
        rule.add_target(targets.LambdaFunction(analyzer_function))

        # Given the analyzer only queries from DynamoDB and performs a little math, it's
        # unlikely it will timeout but we'll track it just in case
        if analyzer_function.timeout:
            cloudwatch.Alarm(self, "AnalyzerAlarm",
                metric=analyzer_function.metric_duration(statistic="Maximum"),
                evaluation_periods=1,
                datapoints_to_alarm=1,
                threshold=analyzer_function.timeout.to_milliseconds(),
                treat_missing_data=cloudwatch.TreatMissingData.IGNORE,
                alarm_name="Analyzer Function Timeout"
            )

        # Grant Analyzer Lambda function access to the parameter store, SNS topic 
        # and DynamoDB
        param.grant_read(analyzer_function)
        param.grant_write(analyzer_function)
        topic.grant_publish(analyzer_function)
        table.grant_read_data(analyzer_function)

        # Grant Query Lambda function access to Secrets Manager, SNS and Dynamodb
        table.grant_write_data(query_function)
        topic.grant_publish(query_function)
        secret.grant_read(query_function)



        # #################################################
        # Everything below this is related to front end
        # #################################################
        
        # ## DYNAMODB

        # frontend_table = dynamodb.Table(self, "FrontendTable",
        #     removal_policy=cdk.RemovalPolicy.DESTROY,
        #     partition_key=dynamodb.Attribute(name="ID", type=dynamodb.AttributeType.STRING),
        #     billing_mode=dynamodb.BillingMode.PROVISIONED
        # )
        # cdk.CfnOutput(self, "FrontEndTableName", value=frontend_table.table_name)
        # cdk.CfnOutput(self, "FrontEndTableARN", value=frontend_table.table_arn)

        # # Lambda function for API Gateway
        # frontend_function = lambda_.Function(self, "FrontendFunction",
        #     runtime=lambda_.Runtime.PYTHON_3_9,
        #     code=lambda_.Code.from_asset("resources"),
        #     handler="tweet_frontend.lambda_handler",
        #     timeout=cdk.Duration.seconds(int(config.frontend_function.TimeoutDuration)),
        #     environment=dict(TABLE=frontend_table.table_name)
        # )
        # cdk.CfnOutput(self, "FrontendFunctionName", value=frontend_function.function_name)
        # cdk.CfnOutput(self, "FrontendFunctionARN", value=frontend_function.function_arn)

        # # API Gateway

        # # There is an option to create a hosted zone or lookup one that already exists.  Since
        # # the zone to be used might exist for other projects, we are assuming it is already
        # # hosted in Route 53 and we can look it up.  The object to this hosted zone will be 
        # # needed for issuing a public certificate.
        # hosted_zone = route53.HostedZone.from_lookup(self, "HostedZone",
        #     domain_name=config.DNS.ZoneName
        # )
        # cdk.CfnOutput(self, "HostedZoneARN", value=hosted_zone.hosted_zone_arn)

        # # Call the Certificate Manager to create a public certificate for the hosted zone.
        # # This is required later for adding custom endpoints to API Gateway and Amplify
        # certificate = acm.Certificate(self, "cert", 
        #     domain_name=config.DNS.APIBaseName,
        #     validation=acm.CertificateValidation.from_dns(hosted_zone)
        # )
        # cdk.CfnOutput(self, "CertificateARN", value=certificate.certificate_arn)

        # # The custom domain will use our domain name as the API endpoint so that we don't
        # # need to hardcode static API Gateway endpoints
        # domain = apigateway.DomainName(self, "CustomDomain",
        #     domain_name=config.DNS.APIBaseName,
        #     certificate=certificate,
        #     endpoint_type=apigateway.EndpointType.EDGE,
        #     security_policy=apigateway.SecurityPolicy.TLS_1_2
        # )
        # cdk.CfnOutput(self, "CustomDomainName", value=domain.domain_name)

        # # A CNAME record is required to create finish off the complete custom domain. In
        # # this case it will create a URL based on api.example_domain.com
        # cname = route53.CnameRecord(self, "ApiCname",
        #     record_name="api",
        #     zone=hosted_zone,
        #     domain_name=domain.domain_name_alias_domain_name      
        # )
        # cdk.CfnOutput(self, "ApiCnameDomain", value=cname.domain_name)

        # # This call will create the API that points at Lambda.  Only a few of the settings 
        # # will be done here.  It is necessary to capture the objects outputed from creation
        # # to be used in subsequent steps.  The deploy=False is necessary to prevent API
        # # Gateway from creating a "prod" stage by devault.  There will be an API with no 
        # # methods or stages
        # api = apigateway.LambdaRestApi(self, "FrontendApi",
        #     handler=frontend_function,
        #     proxy=False,
        #     default_cors_preflight_options=apigateway.CorsOptions(
        #         allow_origins=apigateway.Cors.ALL_ORIGINS,
        #         allow_methods=apigateway.Cors.ALL_METHODS
        #     ),
        #     deploy=False
        # ) 
        # cdk.CfnOutput(self, "FrontendApiName", value=api.rest_api_name)

        # # The Stage call will create the "dev" stage.  It could have been done other ways
        # # but using the Stage call creates the opportunity to save the creation object 
        # # called stage that will be used in base mapping
        # stage = apigateway.Stage(self, "dev",
        #     deployment=apigateway.Deployment(self, "Deployment", api=api),
        #     stage_name="dev"
        # )

        # # The POST method is the only one needed. The integration and method responses
        # # are necessary to pass the JSON and result in a 200 success.  The passthrough
        # # behavior passes the request body for unmapped content types through to the 
        # # integration back end without transformation

        # method = api.root.add_method("POST",
        #     apigateway.LambdaIntegration(frontend_function, proxy=False,
        #         integration_responses=[apigateway.IntegrationResponse(
        #             status_code="200",
        #             response_templates={"application/json": ''}
        #         )],
        #         passthrough_behavior=apigateway.PassthroughBehavior.WHEN_NO_MATCH
        #     ),
        #     method_responses=[apigateway.MethodResponse(
        #         # Successful response from the integration
        #         status_code="200",
        #         # Define what parameters are allowed or not
        #         response_parameters={
        #             "method.response.header.Access-Control-Allow-Origin": True,
        #         }
        #     )]
        # )
        # cdk.CfnOutput(self, "MethodARN", value=method.method_arn)
      
        # # Base mapping creates a base path that clients who call the API must use in 
        # # the invocation URL.  Adding the base_path results in an endpoint in the 
        # # following format: https://api.domain.com/hashtag
        # base_path_mapping = apigateway.BasePathMapping(self, "BasePathMapping",
        #     domain_name=domain,
        #     rest_api=api,
        #     base_path="hashtag",
        #     stage=stage
        # )

        # #  Amplify

        # repo = codecommit.Repository(self, "FrontendRepo",
        #     repository_name="outage_query_front_end",
        #     code=codecommit.Code.from_directory(config.Amplify.WebsiteDist)
        # )
        # cdk.CfnOutput(self, "FrontendRepoName", value=repo.repository_name)
        # cdk.CfnOutput(self, "FrontendRepoARN", value=repo.repository_arn)

        # amplify_app = amplify.App(self, "AmplifyApp",
        #     source_code_provider=amplify.CodeCommitSourceCodeProvider(repository=repo),
        #     auto_branch_deletion=True
        # )
        # cdk.CfnOutput(self, "AmplifyAppName", value=amplify_app.app_name)
        # cdk.CfnOutput(self, "AmplifyAppARN", value=amplify_app.arn)
        # cdk.CfnOutput(self, "AmplifyAppID", value=amplify_app.app_id)

        # main = amplify_app.add_branch("main") 
        # domain = amplify_app.add_domain(config.DNS.ZoneName)
        # domain.map_root(main)
        # domain.map_sub_domain(main, "www")

        # # Grant Front End Lambda full access to DynamoDB
        # frontend_table.grant_full_access(frontend_function)

        # #################################################
        # Everything above this is related to front end
        # #################################################


class HashtagCdkWestStack(cdk.Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        config = load_config('./config.json')

        # Let's get crazy.  There is a query lambda that asks Twitter for Tweets every
        # few minutes and stores data in a DynamoDB table.  That table is replicated to 
        # Oregon.  There is another lambda for querying the DynamoDB table to see if there
        # is an uptick in the Tweets we're looking for.  Since both Lambdas in each region
        # query the same database, we need to build the Oregon lambda to point at the 
        # replicated DynamoDB table.  The problem is that from this stack, we need to know
        # the name of the table that has been replicated into this region.  We're storing
        # the table name in SSM Parameter Store but it's not replicated.  This code will
        # leveage Custom Resources and AWS SDK for looking up the table name across region.

        # This new class SSMParameterReader is an extension of AwsCustomResource.  This
        # extends the functionality while letting us modify it.
        class SSMParameterReader(AwsCustomResource):
            def __init__(self, scope: Construct, name: str, parameterName: str, region: str):
                ssmAwsSdkCall = AwsSdkCall(
                        service="SSM",
                        action="getParameter",
                        parameters={"Name": parameterName},
                        region=region,
                        physical_resource_id=PhysicalResourceId.of(parameterName+'-'+region+'v5')
                    )

                super().__init__(scope, name,
                    on_update=ssmAwsSdkCall, 
                    policy=AwsCustomResourcePolicy.from_sdk_calls(
                            resources=AwsCustomResourcePolicy.ANY_RESOURCE
                        )
                    )
                
            def getParameterValue(self):
                return self.get_response_field('Parameter.Value')

        # Now that the lookup has been defined as part of the extended class above,
        # it's easy to call it and get an answer back.  When we create the lambda,
        # we'll add this table name as an environment variable
        global_table_name = SSMParameterReader(self, "GlobalTableName",
            parameterName="/hashtag_query/global_table_name",
            region="us-east-1"
        ).getParameterValue()

        # Use the global table name to lookup the existing table.  We'll need this 
        # reference later to add read permissions
        table = dynamodb.Table.from_table_name(self, "GlobalTable", global_table_name)
        cdk.CfnOutput(self, "TableARN", value=table.table_arn)
        cdk.CfnOutput(self, "TableName", value=table.table_name)

        # Creating parameter in Systems Manager to store date last notification
        # was sent.  Once notification is sent, don't want to spam.
        param = ssm.StringParameter(self, "LastNotification",
            parameter_name="/hashtag_query/last_notification",
            string_value="0",
            description="Used to track the time the last notificaiton was sent",
            type=ssm.ParameterType.STRING,
            tier=ssm.ParameterTier.STANDARD,
            allowed_pattern=".*"
            )
        cdk.CfnOutput(self, "LastNotificationARN", value=param.parameter_arn)
        cdk.CfnOutput(self, "LastNotificationName", value=param.parameter_name)

        # The SNS topic is used to publish notificaitons when needed
        topic = sns.Topic(self, "SNSTopic",
            topic_name="outage_notifier_topic",
            display_name="Topic used for publishing outage notifications"
            )
        cdk.CfnOutput(self, "TopicARN", value=topic.topic_arn)
        cdk.CfnOutput(self, "TopicName", value=topic.topic_name)

        # Email subscription created for the topic (more people could be added here)
        topic.add_subscription(
            subscriptions.EmailSubscription(config.SNSTopic.EmailSubscription)
            )

        # SMS subscription for topic - requires originating Pinpoint phone number.  Pinpoint
        # not available in Ohio so this is provisioned to us-west-2. By default, a new account
        # is in Sandbox until it has been used and Support case opened to move out. When in the
        # sandbox, we must verify phone numbers we're sending to        
        topic.add_subscription(
            subscriptions.SmsSubscription(config.SNSTopic.SubscriptionPhonenumber)
            )

        # This Lambda function queries DynamoDB, counts the results in hourly bins and 
        # performs a standard deviation.  If standard deviation is off the charts, it calls
        # the SNS topic 
        analyzer_function = lambda_.Function(self, "analyzer_function",
            runtime=lambda_.Runtime.PYTHON_3_9,
            code=lambda_.Code.from_asset("resources"),
            handler="tweet_analyzer.lambda_handler",
            timeout=cdk.Duration.seconds(int(config.analyzer_function.TimeoutDuration)),
            environment=dict(TABLE=global_table_name,
                LASTSENT=param.parameter_name,
                SNSTOPIC=topic.topic_arn)
            )
        cdk.CfnOutput(self, "analyzer_functionARN", value=analyzer_function.function_arn)
        cdk.CfnOutput(self, "analyzer_functionName", value=analyzer_function.function_name)

        # Setting up a cron schedule to trigger the analyzer
        rule = events.Rule(self, "analyzer_schedule",
            schedule=events.Schedule.cron(minute="*/5")
            )
        rule.add_target(targets.LambdaFunction(analyzer_function))

        # Given the analyzer only queries from DynamoDB and performs a little math, it's
        # unlikely it will timeout but we'll track it just in case
        if analyzer_function.timeout:
            cloudwatch.Alarm(self, "analyzer_alarm",
                metric=analyzer_function.metric_duration(statistic="Maximum"),
                evaluation_periods=1,
                datapoints_to_alarm=1,
                threshold=analyzer_function.timeout.to_milliseconds(),
                treat_missing_data=cloudwatch.TreatMissingData.IGNORE,
                alarm_name="Analyzer Function Timeout"
            )

        # Grant Analyzer Lambda function access to the parameter store, SNS topic 
        # and DynamoDB
        param.grant_read(analyzer_function)
        param.grant_write(analyzer_function)
        topic.grant_publish(analyzer_function)
        table.grant_read_data(analyzer_function)



       


        
