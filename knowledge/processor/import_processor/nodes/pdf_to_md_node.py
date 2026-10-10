import logging
import shutil
import time
import zipfile


from knowledge.processor.import_processor.base import BaseNode, T
from knowledge.processor.import_processor.config import ImportConfig
from knowledge.processor.import_processor.exceptions import ValidationError
from knowledge.processor.import_processor.state import ImportGraphState
from pathlib import Path
import requests

class PdfToMdNode(BaseNode):

    name = "pdf_to_md_node"

    def process(self, state: ImportGraphState) -> ImportGraphState:
        #1.上传并轮询MinerU的解析结果
        self.log_step(step_name="Step1", message="上传并轮询MinerU的解析结果")
        pdf_path=state.get("pdf_path")
        pdf_path_obj=Path(pdf_path)
        zip_url=self._upload_pdf_and_query_result(self.config,pdf_path_obj)

        #2.下载ZIP并提取MD文件
        self.log_step(step_name="Step2", message="下载ZIP并提取MD文件")
        file_dir=state.get("file_dir")
        file_dir_obj=Path(file_dir)
        md_path=self._extract_md(zip_url,file_dir_obj,pdf_path_obj)
        #3.将md_path写入state
        state["md_path"]=md_path
        return state

    def _upload_pdf_and_query_result(self,config:ImportConfig,pdf_path_obj:Path)->str:
        #1.检查MinerU的配置是否正确
        mineru_api_token= config.mineru_api_token
        miner_base_url=config.mineru_base_url
        if not miner_base_url or not mineru_api_token:
            self.logger.error("MinerU config is not correct")
            raise ValidationError(node_name=self.name,message="MinerU config is not correct")
        #2.获取文件上传链接
        #2.1构建url
        url=miner_base_url+"/file-urls/batch"
        #2.2构建请求头
        header={
            "Content-Type":"application/json",
            "Authorization":f"Bearer {mineru_api_token}"
        }
        #2.3构建请求体
        data={
            "files":[
                {"name":f"{pdf_path_obj.name}","data_id":"abcd"}
            ],
            "model_version":"vlm"
        }
        #2.4发送请求，获取上面链接
        try:
            response= requests.post(url, headers=header, json=data)
        except Exception as e:
            self.logger.error(f"【获取上传链接】接口调用失败，异常信息：{str(e)}")
            raise RuntimeError(f"【获取上传链接】接口调用失败，异常信息：{str(e)}")

        #2.5检查响应状态码是否为200
        if response.status_code!=200:
            self.logger.error(f"【获取上传链接】接口调用失败，状态码:{response.status_code},响应内容:{response.text}")
            raise RuntimeError(f"【获取上传链接】接口调用失败，状态码:{response.status_code},响应内容:{response.text}")
        #2.6获取到响应结果
        result = response.json()
        #2.7判断业务状态码
        if result["code"]!=0:
            self.logger.error(f"【获取上传链接】接口调用失败，业务状态码:{result['code']},业务状态信息:{result['message']}")
            raise RuntimeError(f"【获取上传链接】接口调用失败，业务状态码:{result['code']},业务状态信息:{result['message']}")
        #2.8获取上传链接
        batch_id = result["data"]["batch_id"]
        upload_url = result["data"]["file_urls"][0]
        self.logger.info(f"batch_id:{batch_id},upload_url:{upload_url}")
        #3.上传PDF文件到mineru
        #3.1发送请求到PDF
        try:
            with open(pdf_path_obj,"rb") as f:
                file_count=f.read()
                res_upload = requests.put(upload_url, data=file_count)
        except Exception as e:
            self.logger.error(f"【上传PDF文件】接口调用失败，异常信息：{str(e)}")
            raise RuntimeError(f"【上传PDF文件】接口调用失败，异常信息：{str(e)}")

        #3.2判断状态码
        if res_upload.status_code!=200:
            self.logger.error(f"【上传PDF文件】接口调用失败，状态码:{res_upload.status_code},响应内容:{res_upload.text}")
            raise RuntimeError(f"【上传PDF文件】接口调用失败，状态码:{res_upload.status_code},响应内容:{res_upload.text}")
        self.logger.info(f"【上传PDF文件】接口调用成功，状态码:{res_upload.status_code},响应内容:{res_upload.text}")
        #4.轮询查询结果，查到成功、失败、超时
        #定义超时时间
        max_time=300
        # 轮询间隔时间
        pull_interval = 3
        #定义耗时
        usage_time=time.time()
        while True:
            end_time=time.time()
            if end_time-usage_time>max_time:
                self.logger.error(f"【轮询解析结果】MinerU 解析超时, batch_id:{batch_id}")
                raise RuntimeError(f"【轮询解析结果】MinerU 解析超时, batch_id:{batch_id}")

            pull_url=miner_base_url+f"/extract-results/batch/{batch_id}"
            pull_res=requests.get(pull_url,headers=header)
            #判断响应状态码，如果不是200，则表示查询失败
            if pull_res.status_code!=200:
                self.logger.error(f"【轮询解析结果】接口调用失败，状态码:{pull_res.status_code},响应内容:{pull_res.text}")
                time.sleep(pull_interval)
                continue
            #判断业务状态码，如果不是0，则表示查询失败
            pull_result=pull_res.json()
            if pull_result["code"]!=0:
                self.logger.error(f"【轮询解析结果】接口调用失败，业务状态码:{pull_result['code']},业务状态信息:{pull_result['message']}")
                time.sleep(pull_interval)
                continue
            #获取解析结果
            extract_result = pull_result['data']['extract_result'][0]
            #获取解析状态
            extract_state = extract_result['state']
            if(extract_state=="done"):
                # 5.返回zip_url
                full_zip_url=extract_result['full_zip_url']
                self.logger.info(f"【轮询解析结果】接口调用成功，解析成功, full_zip_url:{full_zip_url}")
                return full_zip_url
            elif extract_state == "failed":
                # MinerU失败时返回的字段名是err_msg(不是error_msg)，用get安全取值并兜底打印完整结果
                error_msg = extract_result.get("err_msg") or extract_result.get("error_msg") or str(extract_result)
                self.logger.error(f"【轮询解析结果】接口调用失败，解析失败:{error_msg}")
                raise RuntimeError(f"【轮询解析结果】接口调用失败，解析失败:{error_msg}")

            else:
                #表示其他状态，还没成功，继续轮询
                time.sleep(pull_interval)
                continue

    def _extract_md(self,zip_url:str,file_dir_obj:Path,pdf_path_obj:Path)->str:
        #1.发送get请求下载zip包
        try:
            responses=requests.get(zip_url)
        except Exception as e:
            self.logger.error(f"【下载ZIP包】接口调用失败，异常信息：{str(e)}")
            raise RuntimeError(f"【下载ZIP包】接口调用失败，异常信息：{str(e)}")
        if responses.status_code!=200:
            self.logger.error(f"【下载ZIP包】接口调用失败，状态码:{responses.status_code},响应内容:{responses.text}")
            raise RuntimeError(f"【下载ZIP包】接口调用失败，状态码:{responses.status_code},响应内容:{responses.text}")
        self.logger.info(f"【下载ZIP包】接口调用成功，状态码:{responses.status_code}")
        #2.指定zip包的存放路径，将下载下来的zip包写到该路径下
        #2。1构建zip包的存储路径：就是存放在file_dir/文件名_result.zip
        zip_path=file_dir_obj/f"{pdf_path_obj.stem}_result.zip"

        #2.2将response中的内容写入到zip包的存储路径下
        try:
            with open(zip_path,"wb") as f:
                f.write(responses.content)
        except Exception as e:
            self.logger.error(f"【下载ZIP包】接口调用失败，异常信息：{str(e)}")
            raise RuntimeError(f"【下载ZIP包】接口调用失败，异常信息：{str(e)}")
        self.logger.info(f"【下载ZIP包】接口调用成功，zip包存储路径:{zip_path}")
        #3.解压zip包，获取md文件的路径
        #3.1构建zi包的解压路径：file_dir/文件名
        extract_path=file_dir_obj/f"{pdf_path_obj.stem}"
        #3.2如果该目录下已经有内容了，先清楚该目录下的所有内容
        try:
            if extract_path.exists():
                shutil.rmtree(extract_path)
        except Exception as e:
            self.logger.info(f"【解压ZIP包】删除解压路径下已有内容失败，异常信息：{str(e)},不影响继续执行")

        #3.3将zip解压到解压路径下
        try:
            with zipfile.ZipFile(zip_path,"r") as zip_file:
                zip_file.extractall(extract_path)
        except Exception as e:
            self.logger.error(f"【解压ZIP包】接口调用失败，异常信息：{str(e)}")
            raise RuntimeError(f"【解压ZIP包】接口调用失败，异常信息：{str(e)}")
        self.logger.info(f"【解压ZIP包】接口调用成功，解压路径:{extract_path}")
        #3.4将md文件进行重命名：pdf的文件名.md
        target_md_path=extract_path/"full.md"
        new_md_path=target_md_path.rename(extract_path/f"{pdf_path_obj.stem}.md")
        #4.返回md文件的路径

        return str(new_md_path)


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    node=PdfToMdNode()
    state={
        "pdf_path":r"D:\Project\shopkeeper-brain\knowledge\processor\import_processor\input_dir\万用表RS-12的使用.pdf",
        "file_dir":r"D:\Project\shopkeeper-brain\knowledge\processor\import_processor\output_dir"
    }

    state_result=node(state)
    print(state_result)
