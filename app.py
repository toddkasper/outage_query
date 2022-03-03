#!/usr/bin/env python3
import os

import aws_cdk as cdk

from hashtag_cdk.hashtag_cdk_stack import HashtagCdkStack


app = cdk.App()
HashtagCdkStack(app, "HashtagCdkStack")
app.synth()
