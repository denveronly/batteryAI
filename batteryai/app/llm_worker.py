"""Runs one local LLM request in its own process (memory is freed when it exits).

stdin:  {"model_path", "threads", "system", "user", "schema"}
stdout: {"result": {...}, "usage": {...}}
"""

import json
import sys


def main() -> None:
    request = json.load(sys.stdin)
    from llama_cpp import Llama  # imported here so the add-on starts even without llama.cpp

    llm = Llama(
        model_path=request["model_path"],
        n_ctx=8192,
        n_threads=request["threads"],
        verbose=False,
    )
    response = llm.create_chat_completion(
        messages=[
            {"role": "system", "content": request["system"]},
            {"role": "user", "content": request["user"]},
        ],
        # llama.cpp turns the JSON schema into a grammar, so the output always parses.
        response_format={"type": "json_object", "schema": request["schema"]},
        temperature=0.2,
        max_tokens=2048,
    )
    text = response["choices"][0]["message"]["content"]
    json.dump({"result": json.loads(text), "usage": response.get("usage")}, sys.stdout)


if __name__ == "__main__":
    main()
