
# Welcome to Twitter Hashtag Analyzer

This app is designed to be entirely serverless and deployed only through the CDK. 

There only two prerequisites:
1. Twitter API bearer token needs to be in AWS Secrets Manager and the ARN in hashtag_cdk_stack.py
2. A Pinpoint originating number needs to exist in the Region where text messages will be sent via SNS

The application is designed to accomplish the following:
1. On a predefined schedule, query the Twitter API for a search of hashtags
2. Add an entry into DynamoDB for each text with tweet ID being the unique key
3. On a predefined schedule, query DynamoDB, count tweets by hour and calculate standard deviation
4. If standard deviation is relatively huge, send SNS notification

Some notes about this repository:

The `cdk.json` file tells the CDK Toolkit how to execute the app.

The initialization process created a virtualenv within this project, stored 
under the `.venv` directory.  Use the following step to activate your 
virtualenv.

```
$ source .venv/bin/activate
```

If you are a Windows platform, you would activate the virtualenv like this:

```
% .venv\Scripts\activate.bat
```

Once the virtualenv is activated, you can install the required dependencies.

```
$ pip install -r requirements.txt
```

At this point you can now synthesize the CloudFormation template for this code.

```
$ cdk synth
```

To add additional dependencies, for example other CDK libraries, just add
them to your `setup.py` file and rerun the `pip install -r requirements.txt`
command.

## Useful commands

 * `cdk ls`          list all stacks in the app
 * `cdk synth`       emits the synthesized CloudFormation template
 * `cdk deploy`      deploy this stack to your default AWS account/region
 * `cdk diff`        compare deployed stack with current state
 * `cdk docs`        open CDK documentation
