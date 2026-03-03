from vllm_responses.lm import LMEngine, vLLMResponsesRequest


async def main():
    stream = False
    engine = LMEngine(
        body=vLLMResponsesRequest(
            **{
                "input": [
                    {
                        "content": "You have access to tools to get user account number and current savings. Use Python code interpreter tool for all math calculations.",
                        "role": "system",
                    },
                    # {"content": "Where is Paris?", "role": "user"},
                    # {"content": "How much does Sam have?", "role": "user"},
                    {"content": "How much is 2**2.12?", "role": "user"},
                    # {"content": "Hello " * 400_000, "role": "user"},
                ],
                "model": "gpt-4.1-nano",
                # "reasoning": {"effort": "low", "summary": "detailed"},
                "stream": stream,
                "store": False,
                "tool_choice": "required",
                "tools": [
                    {"container": {"type": "auto"}, "type": "code_interpreter"},
                    {
                        "description": "Get account number for a user.",
                        "name": "get_account_number",
                        "parameters": {
                            "additionalProperties": False,
                            "properties": {"username": {"type": "string"}},
                            "required": ["username"],
                            "type": "object",
                        },
                        "strict": True,
                        "type": "function",
                    },
                    {
                        "description": "Get current savings for an account.",
                        "name": "get_current_savings",
                        "parameters": {
                            "additionalProperties": False,
                            "properties": {"account_number": {"type": "string"}},
                            "required": ["account_number"],
                            "type": "object",
                        },
                        "strict": True,
                        "type": "function",
                    },
                ],
            }
        ),
    )
    if stream:
        async for chunk in await engine.run():
            print(chunk)
    else:
        response = await engine.run()
        print(response)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
