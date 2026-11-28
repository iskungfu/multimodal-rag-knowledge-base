from knowledge.investment.retrieval import InvestmentRetriever


class PublicKnowledgeRetriever:
    def __init__(self, retriever=None):
        self.retriever = retriever if retriever is not None else InvestmentRetriever()

    def retrieve(self, query):
        result = self.retriever.retrieve(query, kinds=("knowledge",))["knowledge"]
        items = [item for item in result["items"] if len(item["content"]) <= 3000][:3]
        status = result["status"]
        if status == "retrieved" and not items:
            status = "insufficient"
        return {"status": status, "items": items}
