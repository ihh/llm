from llm import EmbeddingModel, Model, hookimpl
import llm
from llm.utils import dicts_to_table_string
import click
import datetime
import openai

try:
    from pydantic import field_validator, Field  # type: ignore
except ImportError:
    from pydantic.fields import Field
    from pydantic.class_validators import validator as field_validator  # type: ignore [no-redef]
import requests
from typing import List, Iterable, Iterator, Optional, Union
import json
import yaml


@hookimpl
def register_models(register):
    register(Chat("gpt-3.5-turbo"), aliases=("3.5", "chatgpt"))
    register(Chat("gpt-3.5-turbo-16k"), aliases=("chatgpt-16k", "3.5-16k"))
    register(Chat("gpt-4"), aliases=("4", "gpt4"))
    register(Chat("gpt-4-32k"), aliases=("4-32k",))
    # Load extra models
    extra_path = llm.user_dir() / "extra-openai-models.yaml"
    if not extra_path.exists():
        return
    with open(extra_path) as f:
        extra_models = yaml.safe_load(f)
    for extra_model in extra_models:
        model_id = extra_model["model_id"]
        aliases = extra_model.get("aliases", [])
        model_name = extra_model["model_name"]
        api_base = extra_model.get("api_base")
        api_type = extra_model.get("api_type")
        api_version = extra_model.get("api_version")
        api_engine = extra_model.get("api_engine")
        headers = extra_model.get("headers")
        chat_model = Chat(
            model_id,
            model_name=model_name,
            api_base=api_base,
            api_type=api_type,
            api_version=api_version,
            api_engine=api_engine,
            headers=headers,
        )
        if api_base:
            chat_model.needs_key = None
        if extra_model.get("api_key_name"):
            chat_model.needs_key = extra_model["api_key_name"]
        register(
            chat_model,
            aliases=aliases,
        )


@hookimpl
def register_embedding_models(register):
    register(Ada002(), aliases=("ada",))


class Ada002(EmbeddingModel):
    model_id = "ada-002"
    needs_key = "openai"
    key_env_var = "OPENAI_API_KEY"
    batch_size = 100  # Maybe this should be 2048

    def embed_batch(self, items: Iterable[Union[str, bytes]]) -> Iterator[List[float]]:
        results = openai.Embedding.create(
            input=items, model="text-embedding-ada-002", api_key=self.get_key()
        )["data"]
        return ([float(r) for r in result["embedding"]] for result in results)


@hookimpl
def register_commands(cli):
    @cli.group(name="openai")
    def openai_():
        "Commands for working directly with the OpenAI API"

    @openai_.command()
    @click.option("json_", "--json", is_flag=True, help="Output as JSON")
    @click.option("--key", help="OpenAI API key")
    def models(json_, key):
        "List models available to you from the OpenAI API"
        from llm.cli import get_key

        api_key = get_key(key, "openai", "OPENAI_API_KEY")
        response = requests.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if response.status_code != 200:
            raise click.ClickException(
                f"Error {response.status_code} from OpenAI API: {response.text}"
            )
        models = response.json()["data"]
        if json_:
            click.echo(json.dumps(models, indent=4))
        else:
            to_print = []
            for model in models:
                # Print id, owned_by, root, created as ISO 8601
                created_str = datetime.datetime.utcfromtimestamp(
                    model["created"]
                ).isoformat()
                to_print.append(
                    {
                        "id": model["id"],
                        "owned_by": model["owned_by"],
                        "created": created_str,
                    }
                )
            done = dicts_to_table_string("id owned_by created".split(), to_print)
            print("\n".join(done))


class Chat(Model):
    needs_key = "openai"
    key_env_var = "OPENAI_API_KEY"
    can_stream: bool = True

    class Options(llm.Options):
        temperature: Optional[float] = Field(
            description=(
                "What sampling temperature to use, between 0 and 2. Higher values like "
                "0.8 will make the output more random, while lower values like 0.2 will "
                "make it more focused and deterministic."
            ),
            ge=0,
            le=2,
            default=None,
        )
        max_tokens: Optional[int] = Field(
            description="Maximum number of tokens to generate.", default=None
        )
        top_p: Optional[float] = Field(
            description=(
                "An alternative to sampling with temperature, called nucleus sampling, "
                "where the model considers the results of the tokens with top_p "
                "probability mass. So 0.1 means only the tokens comprising the top "
                "10% probability mass are considered. Recommended to use top_p or "
                "temperature but not both."
            ),
            ge=0,
            le=1,
            default=None,
        )
        frequency_penalty: Optional[float] = Field(
            description=(
                "Number between -2.0 and 2.0. Positive values penalize new tokens based "
                "on their existing frequency in the text so far, decreasing the model's "
                "likelihood to repeat the same line verbatim."
            ),
            ge=-2,
            le=2,
            default=None,
        )
        presence_penalty: Optional[float] = Field(
            description=(
                "Number between -2.0 and 2.0. Positive values penalize new tokens based "
                "on whether they appear in the text so far, increasing the model's "
                "likelihood to talk about new topics."
            ),
            ge=-2,
            le=2,
            default=None,
        )
        stop: Optional[str] = Field(
            description=("A string where the API will stop generating further tokens."),
            default=None,
        )
        logit_bias: Optional[Union[dict, str]] = Field(
            description=(
                "Modify the likelihood of specified tokens appearing in the completion. "
                'Pass a JSON string like \'{"1712":-100, "892":-100, "1489":-100}\''
            ),
            default=None,
        )

        @field_validator("logit_bias")
        def validate_logit_bias(cls, logit_bias):
            if logit_bias is None:
                return None

            if isinstance(logit_bias, str):
                try:
                    logit_bias = json.loads(logit_bias)
                except json.JSONDecodeError:
                    raise ValueError("Invalid JSON in logit_bias string")

            validated_logit_bias = {}
            for key, value in logit_bias.items():
                try:
                    int_key = int(key)
                    int_value = int(value)
                    if -100 <= int_value <= 100:
                        validated_logit_bias[int_key] = int_value
                    else:
                        raise ValueError("Value must be between -100 and 100")
                except ValueError:
                    raise ValueError("Invalid key-value pair in logit_bias dictionary")

            return validated_logit_bias

        function_call: Optional[str] = Field(
            description=("The name of a function to return JSON for."),
            default=None,
        )

        functions: Optional[list] = Field(
            description=(
                "Path to JSON file listing functions that the model can return parameters for."
                "The file should contain an array of {name,description,parameters} objects;"
                "each 'parameters' field is a JSON schema, as per the OpenAI API."
            ),
            default=None,
        )

        @field_validator("functions", mode="before")
        def validate_functions(cls, functions):
            if functions is None:
                return None

            if isinstance(functions, str):
                try:
                    with open(functions, "r") as f:
                        functions = json.load(f)
                except json.JSONDecodeError:
                    raise ValueError("Invalid JSON in functions file")

            return functions

    def __init__(
        self,
        model_id,
        key=None,
        model_name=None,
        api_base=None,
        api_type=None,
        api_version=None,
        api_engine=None,
        headers=None,
    ):
        self.model_id = model_id
        self.key = key
        self.model_name = model_name
        self.api_base = api_base
        self.api_type = api_type
        self.api_version = api_version
        self.api_engine = api_engine
        self.headers = headers

    def __str__(self):
        return "OpenAI Chat: {}".format(self.model_id)

    def execute(self, prompt, stream, response, conversation=None):
        messages = []
        current_system = None
        if conversation is not None:
            for prev_response in conversation.responses:
                if (
                    prev_response.prompt.system
                    and prev_response.prompt.system != current_system
                ):
                    messages.append(
                        {"role": "system", "content": prev_response.prompt.system}
                    )
                    current_system = prev_response.prompt.system
                messages.append(
                    {"role": "user", "content": prev_response.prompt.prompt}
                )
                messages.append({"role": "assistant", "content": prev_response.text()})
        if prompt.system and prompt.system != current_system:
            messages.append({"role": "system", "content": prompt.system})
        messages.append({"role": "user", "content": prompt.prompt})
        response._prompt_json = {"messages": messages}
        kwargs = dict(not_nulls(prompt.options))
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_type:
            kwargs["api_type"] = self.api_type
        if self.api_version:
            kwargs["api_version"] = self.api_version
        if self.api_engine:
            kwargs["engine"] = self.api_engine
        if self.needs_key:
            if self.key:
                kwargs["api_key"] = self.key
        else:
            # OpenAI-compatible models don't need a key, but the
            # openai client library requires one
            kwargs["api_key"] = "DUMMY_KEY"
        if self.headers:
            kwargs["headers"] = self.headers
        req_function_call = kwargs.get("function_call")
        if req_function_call is not None:
            kwargs["function_call"] = {"name": req_function_call}
        if stream:
            completion = openai.ChatCompletion.create(
                model=self.model_name or self.model_id,
                messages=messages,
                stream=True,
                **kwargs,
            )
            chunks = []
            for chunk in completion:
                chunks.append(chunk)
                content = chunk["choices"][0].get("delta", {}).get("content")
                if content is not None:
                    yield content
                    continue
                function_call = (
                    chunk["choices"][0].get("delta", {}).get("function_call")
                )
                finish_reason = chunk["choices"][0].get("finish_reason")
                if function_call is not None:
                    function_name = function_call.get("name")
                    if function_name is not None:
                        yield f'{{"function_call": {{\n"name": "{function_name}",\n"arguments": '
                        continue
                    function_args = function_call.get("arguments")
                    if function_args is not None:
                        yield function_args
                        continue
                if finish_reason == "function_call" or (
                    finish_reason == "stop" and req_function_call is not None
                ):
                    yield "\n}}\n"
            response.response_json = combine_chunks(chunks)
        else:
            completion = openai.ChatCompletion.create(
                model=self.model_name or self.model_id,
                messages=messages,
                stream=False,
                **kwargs,
            )
            response.response_json = completion.to_dict_recursive()
            if completion.choices[0].message.content is not None:
                yield completion.choices[0].message.content
            elif "function_call" in completion.choices[0].message:
                function_call = completion.choices[0].message["function_call"]
                function_name = function_call.get("name", "")
                function_args = function_call.get("arguments", "{}")
                try:
                    function_args = json.loads(function_args)
                except json.JSONDecodeError:
                    pass
                yield json.dumps(
                    {
                        "function_call": {
                            "name": function_name,
                            "arguments": function_args,
                        }
                    }
                )
            else:
                yield ""


def not_nulls(data) -> dict:
    return {key: value for key, value in data if value is not None}


def combine_chunks(chunks: List[dict]) -> dict:
    content = ""
    role = None
    finish_reason = None
    function_name = None
    function_args = ""

    for item in chunks:
        for choice in item["choices"]:
            if "role" in choice["delta"]:
                role = choice["delta"]["role"]
            if "content" in choice["delta"] and choice["delta"]["content"] is not None:
                content += choice["delta"]["content"]
            if "function_call" in choice["delta"]:
                if "name" in choice["delta"]["function_call"]:
                    function_name = choice["delta"]["function_call"]["name"]
                if "arguments" in choice["delta"]["function_call"]:
                    function_args += choice["delta"]["function_call"]["arguments"]
            if choice.get("finish_reason") is not None:
                finish_reason = choice["finish_reason"]

    # Coerce the function call back to being content
    if content == "" and function_name is not None:
        content = json.dumps(
            {"function_call": {"name": function_name, "arguments": function_args}}
        )

    # Imitations of the OpenAI API may be missing some of these fields
    combined = {
        "content": content,
        "role": role,
        "finish_reason": finish_reason,
    }
    for key in ("id", "object", "model", "created", "index"):
        if key in chunks[0]:
            combined[key] = chunks[0][key]

    return combined
