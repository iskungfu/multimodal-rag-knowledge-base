import threading
from typing import Optional
from typing import TypeVar, Optional
import logging
logger = logging.getLogger(__name__)


from minio import Minio
from pymongo import MongoClient
from pymilvus import MilvusClient
from dotenv import load_dotenv
from knowledge.utils.clients.base import BaseClientManager

load_dotenv()


class StorageClients(BaseClientManager):
    """存储类客户端：MinIO、Milvus、MongoDB"""

    _minio_client: Optional[Minio] = None
    _minio_lock = threading.Lock()

    _milvus_client: Optional[MilvusClient] = None
    _milvus_lock = threading.Lock()

    _mongo_client: Optional[MongoClient] = None
    _mongo_lock = threading.Lock()

    # ── MinIO ──

    @classmethod
    def get_minio(cls) -> Minio:
        return cls._get_or_create("_minio_client", cls._minio_lock, cls._create_minio)

    @classmethod
    def _create_minio(cls) -> Minio:
        try:
            endpoint = cls._require_env("MINIO_ENDPOINT")
            access_key = cls._require_env("MINIO_ACCESS_KEY")
            secret_key = cls._require_env("MINIO_SECRET_KEY")
            bucket_name = cls._require_env("MINIO_BUCKET_NAME")

            client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=False)

            if not client.bucket_exists(bucket_name):
                client.make_bucket(bucket_name)
                logger.info(f"MinIO bucket '{bucket_name}' 已自动创建")
            else:
                logger.info(f"MinIO bucket '{bucket_name}' 已存在")

            logger.info(f"MinIO 客户端初始化成功 (endpoint={endpoint})")
            return client

        except EnvironmentError:
            raise
        except Exception as e:
            logger.error(f"MinIO 客户端创建失败: {e}")
            raise ConnectionError(f"MinIO 连接失败: {e}") from e

    # ── Milvus ──

    @classmethod
    def get_milvus_client(cls) -> MilvusClient:
        return cls._get_or_create("_milvus_client", cls._milvus_lock, cls._create_milvus_client)

    @classmethod
    def _create_milvus_client(cls) -> MilvusClient:
        try:
            uri = cls._require_env("MILVUS_URL")

            client = MilvusClient(uri=uri)
            logger.info(f"Milvus 客户端初始化成功 (uri={uri})")
            return client

        except EnvironmentError:
            raise
        except Exception as e:
            logger.error(f"Milvus 客户端创建失败: {e}")
            raise ConnectionError(f"Milvus 连接失败: {e}") from e

    # ── MongoDB ──

    @classmethod
    def get_mongo_client(cls) -> MongoClient:
        return cls._get_or_create("_mongo_client", cls._mongo_lock, cls._create_mongo_client)

    @classmethod
    def _create_mongo_client(cls) -> MongoClient:
        try:
            uri = cls._require_env("MONGO_URL")

            # serverSelectionTimeoutMS: 服务不可用时快速失败，避免长时间阻塞
            client = MongoClient(uri, serverSelectionTimeoutMS=5000)
            logger.info("MongoDB 客户端初始化成功")
            return client

        except EnvironmentError:
            raise
        except Exception as e:
            logger.error(f"MongoDB 客户端创建失败: {e}")
            raise ConnectionError(f"MongoDB 连接失败: {e}") from e
