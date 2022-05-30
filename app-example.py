#!/usr/bin/env python3

import os
import aws_cdk as cdk
from hashtag_cdk.hashtag_cdk_stack import HashtagCdkEastStack, HashtagCdkWestStack

env_east = cdk.Environment(account="[Account Number]", region="us-east-1")
env_west = cdk.Environment(account="[Account Number]", region="us-west-2")

app = cdk.App()
HashtagCdkEastStack(app, "EastStack", env=env_east)
HashtagCdkWestStack(app, "WestStack", env=env_west)
app.synth()
