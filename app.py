#!/usr/bin/env python3
import os

import aws_cdk as cdk

from visual_search_stack import VisualSearchStack

app = cdk.App()
VisualSearchStack(app, "VisualSearchStack",
                  env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
                                      region=os.environ.get("CDK_DEFAULT_REGION")))
app.synth()
