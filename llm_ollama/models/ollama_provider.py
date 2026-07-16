import ast
import json
import logging
import uuid

import ollama

from odoo import api, models

from ..utils.ollama_message_validator import OllamaMessageValidator
from ..utils.ollama_tool_call_id_utils import OllamaToolCallIdUtils

_logger = logging.getLogger(__name__)


class LLMProvider(models.Model):
    _inherit = "llm.provider"

    @api.model
    def _get_available_services(self):
        services = super()._get_available_services()
        return services + [("ollama", "Ollama")]

    def ollama_get_client(self):
        """Get Ollama client instance

        If an API key is configured on the provider, it is sent as a Bearer
        token in the Authorization header. This is required for hosted/proxied
        Ollama endpoints (e.g. Ollama Turbo/cloud or an auth gateway) that
        otherwise reject requests with a 401 Unauthorized error.
        """
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        return ollama.Client(
            host=self.api_base or "http://localhost:11434",
            headers=headers or None,
        )

    # Ollama specific implementation
    def ollama_format_tools(self, tools):
        """Format tools for Ollama"""
        return [self._ollama_format_tool(tool) for tool in tools]

    def _ollama_format_tool(self, tool):
        """Convert a tool to Ollama format

        Args:
            tool: llm.tool record to convert

        Returns:
            Dictionary in Ollama tool format
        """
        try:
            # First use the explicit input_schema if available
            if tool.input_schema:
                try:
                    schema = json.loads(tool.input_schema)
                    return self._create_ollama_tool_from_schema(schema, tool)
                except json.JSONDecodeError:
                    _logger.error(f"Invalid JSON schema for tool {tool.name}")
                    # Continue to next approach

            # Next generate schema from the tool's method signature
            schema = tool.get_input_schema()
            if schema:
                return self._create_ollama_tool_from_schema(schema, tool)

            # If we still don't have a schema, use minimal fallback
            _logger.warning(
                f"Could not get schema for tool {tool.name}, using fallback"
            )
            schema = {"type": "object", "properties": {}, "required": []}
            return self._create_ollama_tool_from_schema(schema, tool)

        except Exception as e:
            _logger.error(f"Error formatting tool {tool.name}: {str(e)}")
            # Use minimal fallback schema
            schema = {
                "title": tool.name,
                "description": tool.description,
                "properties": {},
                "required": [],
            }
            return self._create_ollama_tool_from_schema(schema, tool)

    def _recursively_patch_schema_items(self, schema_node):
        """Recursively ensure 'items' dictionaries have a 'type' defined."""
        if not isinstance(schema_node, dict):
            return

        if "items" in schema_node and isinstance(schema_node["items"], dict):
            items_dict = schema_node["items"]
            if "type" not in items_dict:
                items_dict["type"] = "string"
            self._recursively_patch_schema_items(items_dict)

        if "properties" in schema_node and isinstance(schema_node["properties"], dict):
            for prop_schema in schema_node["properties"].values():
                self._recursively_patch_schema_items(prop_schema)

        for combiner in ["anyOf", "allOf", "oneOf"]:
            if combiner in schema_node and isinstance(schema_node[combiner], list):
                for sub_schema in schema_node[combiner]:
                    self._recursively_patch_schema_items(sub_schema)

    def _create_ollama_tool_from_schema(self, schema, tool):
        """Helper method to create an Ollama tool from a schema

        Args:
            schema: JSON schema dictionary
            tool: llm.tool record

        Returns:
            Dictionary in Ollama tool format
        """
        if not schema:
            _logger.warning(
                f"Could not generate schema for tool {tool.name}, skipping."
            )
            return None

        # Ensure all nested 'items' have a 'type' for broader compatibility
        parameters_schema = schema
        self._recursively_patch_schema_items(parameters_schema)

        formatted_tool = {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": {
                    "type": "object",
                    "properties": parameters_schema.get("properties", {}),
                    "required": parameters_schema.get("required", []),
                },
            },
        }

        return formatted_tool

    def _ollama_normalize_message_content(self, messages):
        """Normalize message content to plain strings for Ollama.

        The shared chat-param preparation (and some callers) may set
        ``content`` as a list of parts in the OpenAI multimodal style
        (e.g. tool-consent instructions are injected as
        ``[{"type": "text", "text": ...}]``). Ollama's ``Message`` model
        only accepts a string for ``content``, so flatten any list content
        into a single concatenated string.
        """
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, list):
                text_parts = [
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ]
                non_text = [
                    part.get("type")
                    for part in content
                    if isinstance(part, dict) and part.get("type") != "text"
                ]
                if non_text:
                    _logger.debug(
                        "Ollama does not support non-text content parts; "
                        "dropping parts of types: %s",
                        non_text,
                    )
                msg["content"] = "\n".join(part for part in text_parts if part)

            # Ollama's ToolCall.Function.arguments must be a dict, but stored
            # tool calls (and the OpenAI format) keep arguments as a JSON string.
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    func = tc.get("function")
                    if not isinstance(func, dict):
                        continue
                    arguments = func.get("arguments")
                    if isinstance(arguments, str):
                        try:
                            func["arguments"] = json.loads(arguments) or {}
                        except (json.JSONDecodeError, ValueError):
                            _logger.warning(
                                "Ollama: could not parse tool call arguments "
                                "as JSON, falling back to empty dict: %r",
                                arguments,
                            )
                            func["arguments"] = {"_raw": arguments}
                    elif arguments is None:
                        func["arguments"] = {}
        return messages

    def ollama_chat(
        self,
        messages,
        model=None,
        stream=False,
        tools=None,
        **kwargs,
    ):
        """Send chat messages using Ollama with tools support"""
        model = self.get_model(model, "chat")

        params = self._prepare_chat_params(
            model, messages, stream, tools=tools, **kwargs
        )

        # Ollama's Client.chat() does not support the 'tool_choice' argument,
        # which the generic _prepare_chat_params may add. Remove it to avoid
        # "got an unexpected keyword argument 'tool_choice'" errors.
        params.pop("tool_choice", None)

        # Ollama's Message model requires `content` to be a string, but the
        # shared preparation may inject OpenAI-style list content (e.g. the
        # tool-consent system message). Flatten list content to a string.
        if params.get("messages"):
            params["messages"] = self._ollama_normalize_message_content(
                params["messages"]
            )

        try:
            response = self.client.chat(**params)
        except ollama.ResponseError as e:
            if e.status_code < 500:
                _logger.error(
                    "Ollama client error %d (non-retryable): %s",
                    e.status_code, str(e),
                )
                return {"error": str(e)}
            raise

        if not stream:
            return self.ollama_process_non_streaming_response(response)
        else:
            return self.ollama_process_streaming_response(response)

    def ollama_process_non_streaming_response(self, response):
        """Process a non-streaming response from Ollama"""
        result = {
            "content": response["message"]["content"] or "",  # Handle None content
        }

        if response["message"].get("tool_calls"):
            result["tool_calls"] = []

            for tool_call in response["message"]["tool_calls"]:
                tool_name = tool_call["function"]["name"]
                tool_id = OllamaToolCallIdUtils.create_tool_id(
                    tool_name, str(uuid.uuid4())
                )

                tool_call_data = {
                    "id": tool_id,
                    "type": "function",
                    "function": {"name": tool_name, "arguments": ""},
                }

                if "function" in tool_call and "arguments" in tool_call["function"]:
                    arguments = tool_call["function"]["arguments"]
                    if isinstance(arguments, dict):
                        tool_call_data["function"]["arguments"] = json.dumps(arguments)
                    elif isinstance(arguments, str):
                        tool_call_data["function"]["arguments"] = arguments
                    else:
                        try:
                            tool_call_data["function"]["arguments"] = json.dumps(
                                arguments
                            )
                        except (TypeError, ValueError):
                            _logger.warning(
                                f"Could not serialize arguments of type {type(arguments)}"
                            )
                            tool_call_data["function"]["arguments"] = str(arguments)

                result["tool_calls"].append(tool_call_data)

        if not result.get("content") and not result.get("tool_calls"):
            _logger.warning(
                "Ollama non-streaming response had no content or tool calls."
            )
            return {}

        return result

    def ollama_process_streaming_response(self, response):
        """
        Processes Ollama stream and yields standardized dicts.
        Yields: {'content': str} OR {'tool_calls': list} OR {'error': str}
        """
        assembled_tool_calls = {}
        final_tool_calls_list = []
        stream_has_tools = False
        is_done = False
        last_content = ""

        try:
            for chunk in response:
                if not chunk:
                    continue

                message = chunk.get("message", {})
                content_chunk = message.get("content")
                tool_calls_chunk = message.get("tool_calls")
                chunk_done = chunk.get("done", False)
                error_msg = chunk.get("error")

                if error_msg:
                    yield {"error": f"Ollama stream error: {error_msg}"}
                    return

                if chunk_done:
                    is_done = True

                if content_chunk is not None and content_chunk != last_content:
                    yield {"content": content_chunk}
                    last_content = content_chunk

                if tool_calls_chunk:
                    stream_has_tools = True
                    for tool_call_delta in tool_calls_chunk:
                        func_delta = tool_call_delta.get("function", {})
                        tool_name = func_delta.get("name")
                        key = tool_name if tool_name else str(uuid.uuid4())
                        assembled_tool_calls = self._ollama_update_tool_call_chunk(
                            assembled_tool_calls, tool_call_delta, key
                        )

            if stream_has_tools and is_done:
                for _, call_data in sorted(assembled_tool_calls.items()):
                    if call_data.get("_complete"):
                        tool_name = call_data["function"]["name"]
                        tool_id = OllamaToolCallIdUtils.create_tool_id(
                            tool_name, str(uuid.uuid4())
                        )
                        final_tool_calls_list.append(
                            {
                                "id": tool_id,
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": call_data["function"]["arguments"],
                                },
                            }
                        )

                if final_tool_calls_list:
                    yield {"tool_calls": final_tool_calls_list}

        except Exception as e:
            _logger.error(f"Error processing Ollama stream: {e}", exc_info=True)
            yield {"error": f"Internal error processing Ollama stream: {e}"}

    def _ollama_update_tool_call_chunk(
        self, assembled_tool_calls, tool_call_delta, key
    ):
        """
        Helper to assemble tool calls from Ollama stream chunks.
        Ensures arguments are stored as a complete JSON string.
        """
        if key not in assembled_tool_calls:
            assembled_tool_calls[key] = {
                "id": None,
                "type": "function",
                "function": {"name": "", "arguments": ""},
                "_complete": False,
            }

        current_call = assembled_tool_calls[key]
        func_delta = tool_call_delta.get("function", {})

        if func_delta.get("name"):
            current_call["function"]["name"] = func_delta["name"]

        if "arguments" in func_delta:
            new_args_part = func_delta["arguments"]
            if isinstance(new_args_part, dict):
                # Pre-process dict values: attempt to parse stringified lists
                processed_args = {}
                for arg_key, value in new_args_part.items():
                    if isinstance(value, str):
                        try:
                            parsed_value = ast.literal_eval(value)
                            if isinstance(parsed_value, list):
                                processed_args[arg_key] = parsed_value
                            else:
                                processed_args[arg_key] = value
                        except (ValueError, SyntaxError):
                            processed_args[arg_key] = value
                    else:
                        processed_args[arg_key] = value
                current_call["function"]["arguments"] = json.dumps(processed_args)
            else:
                _logger.warning(
                    f"Unexpected argument type in Ollama stream chunk: {type(new_args_part)}"
                )

        # Use the common helper to determine completeness for Ollama
        current_call["_complete"] = self._is_tool_call_complete(
            current_call["function"], expected_endings=("]", "}")
        )
        return assembled_tool_calls

    def ollama_embedding(self, texts, model=None):
        """Generate embeddings using Ollama"""
        model = self.get_model(model, "embedding")

        # Ensure texts is a list
        if isinstance(texts, str):
            texts = [texts]

        response = self.client.embed(model=model.name, input=texts)
        return response["embeddings"]

    def ollama_models(self, model_id=None):
        """List available Ollama models"""
        if model_id:
            model = self.client.show(model_id)
            yield self._ollama_parse_model(model, model_name=model_id)
        else:
            models_response = self.client.list()
            # Get models from the response
            if hasattr(models_response, "models"):
                models = models_response.models
            else:
                error_msg = f"Unexpected Ollama API response format: {models_response}"
                _logger.error(error_msg)
                raise ValueError(error_msg)

            for model in models:
                yield self._ollama_parse_model(model)

    def _ollama_parse_model(self, model, model_name=None):
        model_name = model_name or model.model

        model_info = {
            "name": model_name,
            "details": {
                "id": model_name,
                "capabilities": ["chat"],  # Default capability
                "modified_at": str(model.modified_at)
                if hasattr(model, "modified_at")
                else None,
                "size": model.size if hasattr(model, "size") else None,
                "digest": model.digest if hasattr(model, "digest") else None,
            },
        }

        # Add embedding capability if model name suggests it
        if "embedding" in model_name.lower():
            model_info["details"]["capabilities"].append("embedding")

        return model_info

    def ollama_format_messages(self, messages, system_prompt=None):
        """Format messages for Ollama API

        Args:
            messages: List of message records
            system_prompt: Optional system prompt to prepend

        Returns:
            List of formatted messages in Ollama format
        """
        # First use the default implementation from the llm_tool module
        formatted_messages = []

        # Add system prompt if provided
        if system_prompt:
            formatted_messages.append({"role": "system", "content": system_prompt})

        # Add all other messages, properly formatted
        for message in messages:
            formatted_msg = self._dispatch("format_message", record=message)
            if formatted_msg is not None:
                formatted_messages.append(formatted_msg)

        # Validate and clean messages
        validator = OllamaMessageValidator(formatted_messages)
        return validator.validate_and_clean()
