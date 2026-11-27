from collections import UserDict
import unittest
from unittest.mock import patch
import uuid

from knowledge.processor.query_processor.nodes.rrf_merge_node import RrfMergeNode


class RetrievalRegressionTests(unittest.TestCase):
    def test_mapping_hits_keep_evidence_across_both_routes(self):
        hit = UserDict({"id": 731, "entity": UserDict({"title": "手册", "content": "校验口令青鹭7391"})})
        result = RrfMergeNode().process({
            "embedding_chunks": [hit],
            "hyde_embedding_chunks": [hit, {"id": 732, "entity": {"content": "其他内容"}}],
        })
        self.assertEqual(len(result["rrf_chunks"]), 2)
        best = result["rrf_chunks"][0]
        self.assertEqual(best["chunk_id"], 731)
        self.assertIn("7391", best["content"])
        self.assertGreater(best["score"], result["rrf_chunks"][1]["score"])

    def test_actual_milvus_hit_keeps_content(self):
        from pymilvus.client.search_result import Hit

        hit = Hit({"id": 731, "distance": 0.9, "entity": {"content": "真实 Hit 数据结构"}})
        self.assertEqual(RrfMergeNode()._format_doc([hit])[0]["content"], "真实 Hit 数据结构")


class StreamRegressionTests(unittest.TestCase):
    def test_generated_stream_answer_is_available_to_polling(self):
        from knowledge.processor.query_processor.nodes.answer_output_node import AnswerOutputNode
        from knowledge.utils.task_util import get_task_result

        task_id = uuid.uuid4().hex
        node = AnswerOutputNode()
        with patch.object(node, "_llm_stream", return_value="完整答案"), patch(
            "knowledge.processor.query_processor.nodes.answer_output_node.AIClients.get_llm_client"
        ):
            node._generate_answer("测试", {"task_id": task_id, "is_stream": True})
        self.assertEqual(get_task_result(task_id, "answer"), "完整答案")

    def test_final_event_closes_http_stream_and_cleans_queue(self):
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        from fastapi import Request
        from fastapi.testclient import TestClient
        from knowledge.utils.sse_util import create_sse_queue, push_sse_event, sse_generator, get_sse_queue

        task_id = uuid.uuid4().hex
        create_sse_queue(task_id)
        push_sse_event(task_id, "delta", {"answer": "答案"})
        push_sse_event(task_id, "final", {"answer": "答案"})
        app = FastAPI()

        @app.get("/stream")
        async def stream(request: Request):
            return StreamingResponse(sse_generator(task_id, request), media_type="text/event-stream")

        with TestClient(app) as client:
            response = client.get("/stream")
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: delta", response.text)
        self.assertEqual(response.text.count("event: final"), 1)
        self.assertIsNone(get_sse_queue(task_id))

    def test_failure_is_visible_and_does_not_expose_exception(self):
        from knowledge.service.query_service import QueryService
        from knowledge.utils.sse_util import create_sse_queue, remove_sse_queue

        for streaming in (False, True):
            task_id = uuid.uuid4().hex
            events = create_sse_queue(task_id)
            with patch("knowledge.service.query_service.query_app") as graph, patch("knowledge.service.query_service.logger"):
                graph.invoke.side_effect = RuntimeError("private-url-and-token")
                QueryService().run_query_graph(task_id, "test", "问题", streaming)
            status = QueryService().get_status(task_id)
            self.assertEqual(status["status"], "failed")
            self.assertTrue(status["error"])
            self.assertNotIn("private", status["error"])
            if streaming:
                event = events.get_nowait()
                self.assertEqual(event["event"], "final")
                self.assertEqual(event["data"]["error"], status["error"])
            remove_sse_queue(task_id)


if __name__ == "__main__":
    unittest.main()
