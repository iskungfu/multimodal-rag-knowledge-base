from knowledge.utils.clients.ai_clients import AIClients

if __name__ == '__main__':
    # 1. 获取重排序模型客户端（单例，由 AIClients 统一管理，模型路径读取 .env 的 BGE_RERANKER_LARGE）
    reranker = AIClients.get_bge_m3_rerank_client()

    # 2. 计算相关性得分
    pairs = [
        ["什么是万用表？", "万用表是一种测量电压、电流、电阻的仪器"],
        ["什么是万用表？", "今天天气很好"],
    ]
    scores = reranker.compute_score(pairs)
    # 输出: [7.8984375, -9.484375]  高分 = 高相关
    print(scores)

    # rrf的结果、web检索结果
