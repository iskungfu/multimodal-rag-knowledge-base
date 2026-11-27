import json
import os
import sys
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import urllib3
from minio import Minio
from pymongo import MongoClient


def check_mongo():
    with MongoClient(os.environ["MONGO_URL"], serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, socketTimeoutMS=5000) as client:
        client.admin.command("ping")
        client[os.environ["MONGO_DB_NAME"]].list_collection_names()


def check_milvus():
    endpoint = urlsplit(os.environ["MILVUS_URL"])
    headers = {"Content-Type": "application/json", "Request-Timeout": "5"}
    if endpoint.username is not None:
        token = f"{unquote(endpoint.username)}:{unquote(endpoint.password or '')}"
        headers["Authorization"] = f"Bearer {token}"
    address = endpoint.netloc.rsplit("@", 1)[-1]
    url = urlunsplit((endpoint.scheme, address, "/v2/vectordb/collections/list", "", ""))
    request = Request(url, data=b"{}", headers=headers, method="POST")
    with urlopen(request, timeout=8) as response:
        result = json.load(response)
    if result.get("code") != 0 or not isinstance(result.get("data"), list):
        raise RuntimeError("MilvusReadFailed")


def check_minio():
    client = Minio(
        os.environ["MINIO_ENDPOINT"],
        access_key=os.environ["MINIO_ACCESS_KEY"],
        secret_key=os.environ["MINIO_SECRET_KEY"],
        secure=False,
        http_client=urllib3.PoolManager(timeout=urllib3.Timeout(connect=5, read=5), retries=False),
    )
    if not client.bucket_exists(os.environ["MINIO_BUCKET_NAME"]):
        raise RuntimeError("ConfiguredBucketMissing")


if __name__ == "__main__":
    if "--stdin" in sys.argv:
        os.environ.update(json.load(sys.stdin))
    failures = []
    for name, check in [("MongoDB", check_mongo), ("Milvus", check_milvus), ("MinIO", check_minio)]:
        try:
            check()
            print(f"{name}: configured read OK", flush=True)
        except Exception as error:
            failures.append(name)
            print(f"{name}: FAILED ({type(error).__name__})", flush=True)
    sys.exit(bool(failures))
