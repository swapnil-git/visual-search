"""Visual Search stack. Backends are picked with CDK context flags (defaults = all-AWS-native):

  -c vector_store=opensearch|solr      (default opensearch = OpenSearch Serverless)
  -c catalog_store=dynamodb|mysql      (default dynamodb)

  solr:   -c solr_url=http://solr.internal:8983  [-c solr_secret_arn=...]  (self-hosted; needs VPC)
  mysql:  -c mysql_secret_arn=arn:...  (JSON secret: host, username, password, dbname, port)
          build the driver layer first: scripts/build_mysql_layer.sh
  VPC (required for solr/mysql unless they are publicly reachable):
          -c vpc_id=vpc-123  -c subnet_ids=subnet-a,subnet-b  -c security_group_ids=sg-1
  Optional: -c search_provisioned=5   (provisioned concurrency for POS latency)

Examples:
  cdk deploy
  cdk deploy -c vector_store=solr -c solr_url=http://10.0.1.5:8983 -c catalog_store=mysql \\
             -c mysql_secret_arn=<arn> -c vpc_id=vpc-123 -c subnet_ids=subnet-a,subnet-b -c security_group_ids=sg-1
"""
import json
import os

from aws_cdk import (
    CfnOutput, Duration, RemovalPolicy, Stack,
    aws_apigateway as apigw,
    aws_dynamodb as ddb,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_lambda_event_sources as sources,
    aws_opensearchserverless as aoss,
    aws_s3 as s3,
    aws_sqs as sqs,
)
from constructs import Construct

COLLECTION = "visual-search"
EMBED_MODEL_ID = "amazon.titan-embed-image-v1"  # verify availability in your region
EMBED_DIM = "1024"
HERE = os.path.dirname(__file__)


class VisualSearchStack(Stack):
    def __init__(self, scope: Construct, cid: str, **kw):
        super().__init__(scope, cid, **kw)
        ctx = self.node.try_get_context
        vector_store = (ctx("vector_store") or "opensearch").lower()
        catalog_store = (ctx("catalog_store") or "dynamodb").lower()
        if vector_store not in ("opensearch", "solr"):
            raise ValueError("vector_store must be opensearch or solr")
        if catalog_store not in ("dynamodb", "mysql"):
            raise ValueError("catalog_store must be dynamodb or mysql")
        if vector_store == "solr" and not ctx("solr_url"):
            raise ValueError("vector_store=solr needs -c solr_url=...")
        if catalog_store == "mysql" and not ctx("mysql_secret_arn"):
            raise ValueError("catalog_store=mysql needs -c mysql_secret_arn=...")

        # ---------------------------------------------------------------- storage
        bucket = s3.Bucket(
            self, "ImageBucket", encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[s3.LifecycleRule(prefix="queries/", expiration=Duration.days(1))])

        def role(name):
            return iam.Role(self, name, assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(
                                "service-role/AWSLambdaBasicExecutionRole")])

        search_role, ingest_role = role("SearchRole"), role("IngestRole")
        roles = (search_role, ingest_role)
        env = {"EMBEDDER": "bedrock", "EMBED_MODEL_ID": EMBED_MODEL_ID, "EMBED_DIM": EMBED_DIM,
               "IMAGE_BUCKET": bucket.bucket_name, "VECTOR_STORE": vector_store,
               "CATALOG_STORE": catalog_store, "VECTOR_INDEX": "products"}

        for r in roles:
            r.add_to_policy(iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[f"arn:aws:bedrock:{self.region}::foundation-model/{EMBED_MODEL_ID}"]))
        bucket.grant_read(search_role, "queries/*")
        bucket.grant_read(ingest_role, "catalog/*")
        search_role.add_to_policy(iam.PolicyStatement(actions=["rekognition:DetectText"], resources=["*"]))

        # ---------------------------------------------------------------- vector store
        coll = None
        if vector_store == "opensearch":
            enc = aoss.CfnSecurityPolicy(
                self, "EncPolicy", name=f"{COLLECTION}-enc", type="encryption",
                policy=json.dumps({"Rules": [{"ResourceType": "collection",
                                              "Resource": [f"collection/{COLLECTION}"]}], "AWSOwnedKey": True}))
            net = aoss.CfnSecurityPolicy(
                self, "NetPolicy", name=f"{COLLECTION}-net", type="network",
                # Public endpoint, still IAM (SigV4) authenticated. For production prefer a VPC endpoint.
                policy=json.dumps([{"Rules": [{"ResourceType": "collection",
                                               "Resource": [f"collection/{COLLECTION}"]}],
                                    "AllowFromPublic": True}]))
            coll = aoss.CfnCollection(self, "Collection", name=COLLECTION, type="VECTORSEARCH")
            coll.add_dependency(enc)
            coll.add_dependency(net)
            index_res = [f"index/{COLLECTION}/*"]
            aoss.CfnAccessPolicy(
                self, "DataPolicy", name=f"{COLLECTION}-data", type="data",
                policy=json.dumps([
                    {"Rules": [{"ResourceType": "index", "Resource": index_res,
                                "Permission": ["aoss:DescribeIndex", "aoss:ReadDocument"]}],
                     "Principal": [search_role.role_arn]},
                    {"Rules": [{"ResourceType": "index", "Resource": index_res,
                                "Permission": ["aoss:CreateIndex", "aoss:DescribeIndex", "aoss:UpdateIndex",
                                               "aoss:ReadDocument", "aoss:WriteDocument"]},
                               {"ResourceType": "collection", "Resource": [f"collection/{COLLECTION}"],
                                "Permission": ["aoss:DescribeCollectionItems"]}],
                     "Principal": [ingest_role.role_arn]}]))
            for r in roles:
                r.add_to_policy(iam.PolicyStatement(actions=["aoss:APIAccessAll"], resources=[coll.attr_arn]))
            env["AOSS_ENDPOINT"] = coll.attr_collection_endpoint
        else:
            env["SOLR_URL"] = ctx("solr_url")
            env["SOLR_COLLECTION"] = ctx("solr_collection") or "products"
            if ctx("solr_secret_arn"):
                env["SOLR_SECRET_ARN"] = ctx("solr_secret_arn")
                for r in roles:
                    r.add_to_policy(iam.PolicyStatement(actions=["secretsmanager:GetSecretValue"],
                                                        resources=[ctx("solr_secret_arn")]))

        # ---------------------------------------------------------------- catalog store
        layers = []
        registry_tbl = None
        if catalog_store == "dynamodb":
            def table(name, pk, sk=None):
                return ddb.Table(
                    self, name, billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
                    partition_key=ddb.Attribute(name=pk, type=ddb.AttributeType.STRING),
                    sort_key=ddb.Attribute(name=sk, type=ddb.AttributeType.STRING) if sk else None,
                    point_in_time_recovery=True, removal_policy=RemovalPolicy.RETAIN)
            products, variants = table("Products", "product_id"), table("Variants", "product_id", "sku")
            registry_tbl = table("IndexRegistry", "product_id", "color")
            variants.add_global_secondary_index(
                index_name="barcode-index",
                partition_key=ddb.Attribute(name="barcode", type=ddb.AttributeType.STRING))
            products.grant_read_data(search_role)
            variants.grant_read_data(search_role)
            for t in (products, variants, registry_tbl):
                t.grant_read_write_data(ingest_role)
            env.update({"PRODUCTS_TABLE": products.table_name, "VARIANTS_TABLE": variants.table_name,
                        "REGISTRY_TABLE": registry_tbl.table_name, "VARIANTS_BARCODE_INDEX": "barcode-index"})
        else:
            layer_dir = os.path.join(HERE, "layers", "pymysql")
            if not os.path.isdir(os.path.join(layer_dir, "python")):
                raise RuntimeError("MySQL driver layer missing: run scripts/build_mysql_layer.sh first")
            layers.append(_lambda.LayerVersion(
                self, "PyMySqlLayer", code=_lambda.Code.from_asset(layer_dir),
                compatible_runtimes=[_lambda.Runtime.PYTHON_3_12],
                compatible_architectures=[_lambda.Architecture.ARM_64]))
            env["MYSQL_SECRET_ARN"] = ctx("mysql_secret_arn")
            for r in roles:
                r.add_to_policy(iam.PolicyStatement(actions=["secretsmanager:GetSecretValue"],
                                                    resources=[ctx("mysql_secret_arn")]))

        # ---------------------------------------------------------------- optional VPC
        vpc_kw = {}
        if ctx("vpc_id"):
            vpc = ec2.Vpc.from_vpc_attributes(
                self, "Vpc", vpc_id=ctx("vpc_id"),
                availability_zones=self.availability_zones[:len(ctx("subnet_ids").split(","))])
            vpc_kw = {
                "vpc": vpc,
                "vpc_subnets": ec2.SubnetSelection(subnets=[
                    ec2.Subnet.from_subnet_id(self, f"Sn{i}", s)
                    for i, s in enumerate(ctx("subnet_ids").split(","))]),
                "security_groups": [ec2.SecurityGroup.from_security_group_id(self, f"Sg{i}", g)
                                    for i, g in enumerate((ctx("security_group_ids") or "").split(",")) if g]}
            for r in roles:
                r.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaVPCAccessExecutionRole"))
            # Lambdas in private subnets need NAT or VPC endpoints for Bedrock, Rekognition, S3, Secrets.

        # ---------------------------------------------------------------- lambdas
        code = _lambda.Code.from_asset(os.path.join(HERE, "lambdas"), exclude=["requirements*.txt", "__pycache__"])

        def fn(name, handler, role_, timeout, extra_env=None):
            return _lambda.Function(
                self, name, runtime=_lambda.Runtime.PYTHON_3_12, architecture=_lambda.Architecture.ARM_64,
                handler=handler, code=code, role=role_, memory_size=1024, timeout=Duration.seconds(timeout),
                environment={**env, **(extra_env or {})}, layers=layers, **vpc_kw)

        search_fn = fn("SearchFn", "search.handler", search_role, 15)
        inventory_fn = fn("InventoryFn", "inventory.handler", search_role, 15)

        dlq = sqs.Queue(self, "IngestDLQ", retention_period=Duration.days(14))
        queue = sqs.Queue(self, "IngestQueue", visibility_timeout=Duration.seconds(720),
                          dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=dlq))
        ingest_fn = fn("IngestFn", "ingest.handler", ingest_role, 120)
        # max_concurrency caps embedding throughput so we stay under the Bedrock quota
        ingest_fn.add_event_source(sources.SqsEventSource(
            queue, batch_size=10, max_concurrency=10, report_batch_item_failures=True))

        def target(f):
            prov = int(ctx("search_provisioned") or 0)
            return f.current_version.add_alias("live", provisioned_concurrent_executions=prov) if prov > 0 else f

        # ---------------------------------------------------------------- API
        api = apigw.RestApi(
            self, "Api", rest_api_name="visual-search",
            deploy_options=apigw.StageOptions(stage_name="v1", throttling_rate_limit=200,
                                              throttling_burst_limit=400, tracing_enabled=True))
        api.root.add_resource("search").add_method(
            "POST", apigw.LambdaIntegration(target(search_fn)), api_key_required=True)
        api.root.add_resource("inventory").add_resource("check").add_method(
            "POST", apigw.LambdaIntegration(target(inventory_fn)), api_key_required=True)

        for client in ("pos", "ecom"):
            key = api.add_api_key(f"{client}-key", api_key_name=f"visual-search-{client}")
            plan = api.add_usage_plan(f"{client}-plan", name=f"visual-search-{client}",
                                      throttle=apigw.ThrottleSettings(rate_limit=100, burst_limit=200))
            plan.add_api_stage(stage=api.deployment_stage)
            plan.add_api_key(key)

        CfnOutput(self, "SearchUrl", value=f"{api.url}search")
        CfnOutput(self, "InventoryCheckUrl", value=f"{api.url}inventory/check")
        CfnOutput(self, "ImageBucketName", value=bucket.bucket_name)
        CfnOutput(self, "IngestQueueUrl", value=queue.queue_url)
        CfnOutput(self, "VectorStore", value=vector_store)
        CfnOutput(self, "CatalogStore", value=catalog_store)
