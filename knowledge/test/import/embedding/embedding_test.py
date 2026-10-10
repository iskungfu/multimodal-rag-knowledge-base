from nltk.corpus.reader import documents
from pymilvus.model.hybrid import BGEM3EmbeddingFunction

# 1. 加载模型
bge_m3 = BGEM3EmbeddingFunction(
    model_name=r"D:\develop\ai_models\embedding_models\bge-m3",
    # 如果安装的torch是gpu版本，这里就是"cuda:0",如果安装的torch是cpu版本，这里就是cpu
    device="cpu",
    # 如果安装的torch是gpu版本，这里就是True，如果安装的是cpu版本，这里就是False
    use_fp16=False,
)

# 2. 生成嵌入
document_list=["我是美国人","我是中国人"]
embeddings = bge_m3.encode_documents(document_list)

# 3. 提取向量
# dense_vector = embeddings["dense"][0].tolist()   # List[float], 长度 1024
dense=embeddings.get("dense")
dense_vector_list=[]
for item in dense:
    dense_vector=item.tolist()
    dense_vector_list.append(dense_vector)

print(dense_vector_list)
sparse_matrix = embeddings["sparse"]            # CSR 稀疏矩阵
# indices:表示稀疏向量的token_id，也就是不为0的维度
# data:表示每个token_id的权重
# inptr:每个稀疏向量的起始和终止（不包含）的下标
"""
我们最终要得到的稀疏向量应该是：
{
    "token_id:"权重"
}
"""

# # 4. 从 CSR 矩阵提取稀疏向量
# 获取开始和结束的下标
indptr=sparse_matrix.indptr
indices = sparse_matrix.indices
data = sparse_matrix.data
sparse_vector_list = []
for i in range(0,len(document_list)):
    start_index=indptr[i]
    end_index=indptr[i+1]
    token_ids = indices[start_index:end_index].tolist()
    weights = data[start_index:end_index].tolist()
    sparse_vector_list.append(dict(zip(token_ids, weights)))
print(sparse_vector_list)

# start_idx = sparse_matrix.indptr[0]
# end_idx = sparse_matrix.indptr[1]
# token_ids = sparse_matrix.indices[start_idx:end_idx].tolist()
# weights = sparse_matrix.data[start_idx:end_idx].tolist()
# sparse_vector = dict(zip(token_ids, weights))    # Dict[int, float]
# print(sparse_vector)