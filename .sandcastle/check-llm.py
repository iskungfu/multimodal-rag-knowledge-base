import json
import os
import sys
from urllib.request import Request, urlopen


def check_llm():
    request = Request(
        os.environ["OPEN_API_BASE"].rstrip("/") + "/chat/completions",
        data=json.dumps({
            "model": os.environ["LLM_DEFAULT_MODEL"],
            "messages": [{"role": "user", "content": "Reply exactly APP_LLM_OK"}],
        }).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["OPEN_API_KEY"]},
        method="POST",
    )
    with urlopen(request, timeout=60) as response:
        result = json.load(response)
    content = result["choices"][0]["message"].get("content") or ""
    if "APP_LLM_OK" not in content:
        raise RuntimeError("UnexpectedModelResponse")


if __name__ == "__main__":
    try:
        check_llm()
        print("Application LLM: real response OK", flush=True)
    except Exception as error:
        print(f"Application LLM: FAILED ({type(error).__name__})", flush=True)
        sys.exit(1)
