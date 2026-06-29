import io
import json
import logging
import uuid

from openai import APIStatusError, OpenAI

from odoo import api, models
from odoo.exceptions import UserError

from ..utils.openai_message_validator import OpenAIMessageValidator

_logger = logging.getLogger(__name__)

OPENAI_TO_ODOO_STATE_MAPPING = {
    "validating_files": "validating",
    "preparing": "preparing",
    "queued": "queued",
    "running": "training",
    "succeeded": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}


class LLMProvider(models.Model):
    _inherit = "llm.provider"

    @api.model
    def _get_available_services(self):
        services = super()._get_available_services()
        return services + [("openai", "OpenAI")]

    def openai_get_client(self):
        """Get OpenAI client instance"""
        return OpenAI(api_key=self.api_key, base_url=self.api_base or None)

    # OpenAI specific implementation
    def openai_format_tools(self, tools):
        """Format tools for OpenAI"""
        return [self._openai_format_tool(tool) for tool in tools]

    def _openai_format_tool(self, tool):
        """Convert a tool to OpenAI format

        Args:
            tool: llm.tool record to convert

        Returns:
            Dictionary in OpenAI tool format
        """
        try:
            if tool.input_schema:
                try:
                    schema = json.loads(tool.input_schema)
                    return self._create_openai_tool_from_schema(schema, tool)
                except json.JSONDecodeError:
                    _logger.error(f"Invalid JSON schema for tool {tool.name}")

            schema = tool.get_input_schema()
            if schema:
                return self._create_openai_tool_from_schema(schema, tool)

            _logger.warning(
                f"Could not get schema for tool {tool.name}, using fallback"
            )
            schema = {"type": "object", "properties": {}, "required": []}
            return self._create_openai_tool_from_schema(schema, tool)

        except Exception as e:
            _logger.error(f"Error formatting tool {tool.name}: {str(e)}")
            schema = {
                "title": tool.name,
                "description": tool.description,
                "properties": {},
                "required": [],
            }
            return self._create_openai_tool_from_schema(schema, tool)

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

    def _create_openai_tool_from_schema(self, schema, tool):
        """Convert a JSON schema dictionary to an OpenAI tool format,
        patching missing item types recursively.
        Args:
            schema: JSON schema dictionary
            tool: llm.tool record

        Returns:
            Dictionary in OpenAI tool format
        """
        if not schema:
            _logger.warning(
                f"Could not generate schema for tool {tool.name}, skipping."
            )
            return None

        # Ensure all nested 'items' have a 'type' for broader compatibility
        parameters_schema = schema  # Modify the schema directly before formatting
        self._recursively_patch_schema_items(parameters_schema)

        # Format according to OpenAI requirements
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

    def openai_chat(
        self,
        messages,
        model=None,
        stream=False,
        tools=None,
        tool_choice="auto",
        prepend_messages=None,
    ):
        """Send chat messages using OpenAI with tools support"""
        model = self.get_model(model, "chat")

        # Prepare request parameters
        params = self._prepare_chat_params(
            model,
            messages,
            stream,
            tools=tools,
            prepend_messages=prepend_messages,
            tool_choice=tool_choice,
        )

        try:
            response = self.client.chat.completions.create(**params)
        except APIStatusError as e:
            if e.status_code < 500:
                _logger.error(
                    "OpenAI client error %d (non-retryable): %s",
                    e.status_code, str(e),
                )
                return {"error": str(e)}
            raise

        # Process the response based on streaming mode
        if not stream:
            return self._openai_process_non_streaming_response(response)
        else:
            return self._openai_process_streaming_response(response)

    def _openai_process_non_streaming_response(self, response):
        """Processes OpenAI non-streamed response and returns ONE standardized dict."""
        _logger.info("Processing non-streaming OpenAI response.")
        try:
            choice = response.choices[0]
            message = choice.message
            result = {}

            if message.content:
                result["content"] = message.content

            message_ts = self._extract_thought_signature_from_message(message)
            if message_ts:
                result["thought_signature"] = message_ts

            if message.tool_calls:
                result["tool_calls"] = []
                for tc in message.tool_calls:
                    tc_dict = {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    thought_signature = self._extract_thought_signature(tc)
                    if thought_signature:
                        tc_dict["thought_signature"] = thought_signature
                    result["tool_calls"].append(tc_dict)

            if "content" in result or "tool_calls" in result:
                return result
            else:
                _logger.warning(
                    "OpenAI non-streaming response had no content or tool calls."
                )
                return {}

        except (AttributeError, IndexError, Exception) as e:
            _logger.exception("Error processing OpenAI non-streaming response")
            return {"error": f"Error processing response: {e}"}

    def _openai_process_streaming_response(self, response_stream):
        """
        Processes OpenAI stream and yields standardized dicts for start_thread_loop.
        Yields: {'content': str} OR {'tool_calls': list} OR {'error': str}
        """
        assembled_tool_calls = {}
        final_tool_calls_list = []
        stream_has_tools = False
        finish_reason = None

        try:
            accumulated_thought_signature = None
            for chunk in response_stream:
                choice = chunk.choices[0] if chunk.choices else None
                delta = choice.delta if choice else None
                chunk_finish_reason = choice.finish_reason if choice else None
                if chunk_finish_reason:
                    finish_reason = chunk_finish_reason

                if not delta:
                    continue

                # Extract thought_signature from delta-level extra_content
                # (Gemini returns it here for thinking/text parts)
                delta_ts = self._extract_thought_signature_from_message(delta)
                if delta_ts:
                    accumulated_thought_signature = delta_ts

                if delta.content:
                    yield {"content": delta.content}

                if delta.tool_calls:
                    stream_has_tools = True
                    # index can be null, so we use a counter as fallback
                    call_counter = 0
                    for tool_call_chunk in delta.tool_calls:
                        index = tool_call_chunk.index or call_counter
                        assembled_tool_calls = self._update_openai_tool_call_chunk(
                            assembled_tool_calls, tool_call_chunk, index
                        )
                        call_counter += 1
            if stream_has_tools:
                if finish_reason == "tool_calls" or (
                    finish_reason != "error" and assembled_tool_calls
                ):
                    for index, call_data in sorted(assembled_tool_calls.items()):
                        if call_data.get("_complete"):
                            tool_call_id = call_data.get("id").strip() or str(
                                uuid.uuid4()
                            )
                            tc_dict = {
                                "id": tool_call_id,
                                "type": call_data.get(
                                    "type", "function"
                                ),
                                "function": {
                                    "name": call_data["function"]["name"],
                                    "arguments": call_data["function"]["arguments"],
                                },
                            }
                            if call_data.get("thought_signature"):
                                tc_dict["thought_signature"] = call_data["thought_signature"]
                            final_tool_calls_list.append(tc_dict)
                        else:
                            yield {
                                "error": f"Received incomplete tool call data from provider for tool index {index}."
                            }

                    if final_tool_calls_list:
                        result = {"tool_calls": final_tool_calls_list}
                        if accumulated_thought_signature:
                            result["thought_signature"] = accumulated_thought_signature
                        yield result
                    elif assembled_tool_calls:
                        _logger.warning(
                            "Stream indicated tool calls, but none were successfully assembled."
                        )

                elif finish_reason != "error":
                    _logger.warning(
                        f"OpenAI stream had tool chunks but finished with reason '{finish_reason}'. Not yielding tool calls."
                    )

        except Exception as e:
            yield {"error": f"Internal error processing stream: {e}"}

    def _update_openai_tool_call_chunk(self, tool_call_chunks, tool_call_chunk, index):
        """
        Helper to assemble fragmented tool calls from OpenAI stream chunks.
        (Keep this helper as it's essential for stream processing)
        """
        if index not in tool_call_chunks:
            tool_call_chunks[index] = {
                "id": tool_call_chunk.id,
                "type": tool_call_chunk.type,
                "function": {"name": "", "arguments": ""},
                "_complete": False,
            }

        current_call = tool_call_chunks[index]

        if tool_call_chunk.id:
            current_call["id"] = tool_call_chunk.id
        if tool_call_chunk.type:
            current_call["type"] = tool_call_chunk.type

        thought_signature = self._extract_thought_signature(tool_call_chunk)
        if thought_signature:
            current_call["thought_signature"] = thought_signature

        func_chunk = tool_call_chunk.function
        if func_chunk:
            if func_chunk.name:
                current_call["function"]["name"] = func_chunk.name
            if func_chunk.arguments:
                current_call["function"]["arguments"] += func_chunk.arguments

        # Use the common helper to determine completeness for OpenAI
        current_call["_complete"] = self._is_tool_call_complete(
            current_call["function"], expected_endings=("]", "}")
        )

        return tool_call_chunks

    _thought_signature_registry = {}

    def _detect_thought_signature_required(self):
        """Check if this provider has returned thought_signature in tool call responses.

        The Gemini API includes a thought_signature field in functionCall parts that
        must be preserved when sending tool calls back in conversation history.
        Once a provider is detected as requiring thought_signature, all tool calls
        sent to it will include the preserved thought_signature values.

        Detection happens in two ways:
        1. Proactive: by checking if the api_base matches known Gemini endpoints
        2. Reactive: by detecting thought_signature in API responses

        State is tracked in a class-level dict keyed by record ID because
        Odoo recordsets do not support arbitrary instance attributes.
        """
        key = self.id
        if key in self._thought_signature_registry:
            return True

        if self._is_gemini_endpoint():
            self._thought_signature_registry[key] = True
            return True

        return False

    def _is_gemini_endpoint(self):
        """Check if this provider's api_base points to a Gemini OpenAI-compatible endpoint."""
        api_base = (self.api_base or '').rstrip('/')
        gemini_patterns = (
            'generativelanguage.googleapis.com',
            'aiplatform.googleapis.com',
        )
        return any(pattern in api_base for pattern in gemini_patterns)

    def _set_thought_signature_detected(self):
        """Mark this provider as requiring thought_signature support."""
        LLMProvider._thought_signature_registry[self.id] = True

    def _extract_thought_signature(self, tool_call_obj):
        """Extract thought_signature from a tool call object for Gemini API compatibility.

        Auto-detects when the provider endpoint requires thought_signature by checking
        if the response includes the field. Once detected, subsequent message formatting
        will preserve thought_signature in tool calls.

        The Gemini OpenAI-compatible endpoint returns thought_signature in various
        locations depending on the response mode:
        - Non-streaming: inside ``model_extra`` or ``google`` attribute on the tool call
        - Streaming: inside ``extra_content.google.thought_signature`` on the delta
        """
        if not tool_call_obj:
            return None

        # 1. Check direct attribute (non-Gemini endpoints that include it top-level)
        thought_signature = getattr(tool_call_obj, 'thought_signature', None)
        if thought_signature is not None:
            self._set_thought_signature_detected()
            return thought_signature

        extra = getattr(tool_call_obj, 'model_extra', None) or {}

        # 2. Check model_extra for top-level thought_signature
        result = extra.get('thought_signature')
        if result:
            self._set_thought_signature_detected()
            return result

        # 3. Check extra_content.google.thought_signature (Gemini streaming format)
        extra_content = getattr(tool_call_obj, 'extra_content', None) or extra.get('extra_content', {})
        if isinstance(extra_content, dict):
            google_data = extra_content.get('google', {})
            if isinstance(google_data, dict):
                result = google_data.get('thought_signature')
                if result:
                    self._set_thought_signature_detected()
                    return result

        # 4. Check google-nested thought_signature (Gemini non-streaming format)
        google_data = getattr(tool_call_obj, 'google', None) or extra.get('google', {})
        if isinstance(google_data, dict):
            result = google_data.get('thought_signature')
            if result:
                self._set_thought_signature_detected()
                return result

        return None

    def _extract_thought_signature_from_message(self, message_obj):
        """Extract thought_signature from a chat completion message object.

        Gemini can return thought_signature at the message level (not on tool_calls)
        via extra_content.google.thought_signature on the message/delta object.
        """
        if not message_obj:
            return None

        extra = getattr(message_obj, 'model_extra', None) or {}

        # Check extra_content.google.thought_signature
        extra_content = getattr(message_obj, 'extra_content', None) or extra.get('extra_content', {})
        if isinstance(extra_content, dict):
            google_data = extra_content.get('google', {})
            if isinstance(google_data, dict):
                result = google_data.get('thought_signature')
                if result:
                    self._set_thought_signature_detected()
                    return result

        # Check google attribute directly
        google_data = getattr(message_obj, 'google', None) or extra.get('google', {})
        if isinstance(google_data, dict):
            result = google_data.get('thought_signature')
            if result:
                self._set_thought_signature_detected()
                return result

        return None

    def openai_embedding(self, texts, model=None):
        """Generate embeddings using OpenAI"""
        model = self.get_model(model, "embedding")

        response = self.client.embeddings.create(model=model.name, input=texts)
        return [r.embedding for r in response.data]

    def openai_models(self, model_id=None):
        """List available OpenAI models"""
        if model_id:
            model = self.client.models.retrieve(model_id)
            yield self._openai_parse_model(model)
        else:
            models = self.client.models.list()
            for model in models.data:
                yield self._openai_parse_model(model)

    def _openai_parse_model(self, model):
        capabilities = ["chat"]  # default
        if "text-embedding" in model.id:
            capabilities = ["embedding"]
        elif "gpt-4-vision" in model.id:
            capabilities = ["chat", "multimodal"]

        return {
            "name": model.id,
            "details": {
                "id": model.id,
                "capabilities": capabilities,
                **model.model_dump(),
            },
        }

    def _validate_and_clean_messages(self, messages):
        """
        Validate and clean messages to ensure proper tool message structure for OpenAI.

        This method uses the OpenAIMessageValidator class to check that all tool messages
        have a preceding assistant message with matching tool_calls, and removes any
        tool messages that don't meet this requirement to avoid API errors.

        Args:
            messages (list): List of messages to validate and clean

        Returns:
            list: Cleaned list of messages
        """
        # Hardcoded value for verbose logging
        verbose_logging = False

        validator = OpenAIMessageValidator(
            messages, logger=_logger, verbose_logging=verbose_logging
        )
        return validator.validate_and_clean()

    def openai_format_messages(self, messages, system_prompt=None):
        """Format messages for OpenAI API

        Args:
            messages: mail.message recordset to format
            system_prompt: Optional system prompt (deprecated, use prepend_messages)

        Returns:
            List of formatted messages in OpenAI-compatible format
        """
        # Format the messages
        formatted_messages = []

        # Add system prompt if provided (for backward compatibility)
        if system_prompt:
            formatted_messages.append({"role": "system", "content": system_prompt})

        # Format the rest of the messages
        for message in messages:
            formatted_message = self._dispatch("format_message", record=message)
            if formatted_message:
                formatted_messages.append(formatted_message)

        # Then validate and clean the messages for OpenAI
        result_messages = self._validate_and_clean_messages(formatted_messages)

        # Detect if thought_signature is required from any pre-existing tool calls
        thought_signature_required = self._detect_thought_signature_required()
        if not thought_signature_required:
            for msg in result_messages:
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        if "thought_signature" in tc:
                            thought_signature_required = True
                            self._set_thought_signature_detected()
                            break
                    if thought_signature_required:
                        break

        if thought_signature_required:
            self._apply_thought_signature_to_messages(result_messages)
        else:
            for msg in result_messages:
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        tc.pop("thought_signature", None)

        return result_messages

    def _apply_thought_signature_to_messages(self, messages):
        """Convert top-level thought_signature on tool_calls to extra_content.google format.

        The Gemini OpenAI-compatible endpoint requires thought_signature to be sent
        inside ``extra_content.google.thought_signature`` on each tool call item.
        The OpenAI Python SDK strips unknown top-level keys from tool_calls, but
        ``extra_content`` is a known extension field that is preserved.

        This method also extracts thought_signature from content-level extra_content
        on assistant messages and moves it into the proper tool_calls format.
        """
        for msg in messages:
            if msg.get("role") != "assistant":
                continue

            # Handle assistant content-level extra_content (Gemini thought signatures
            # on text/thinking parts are sometimes returned at the delta level)
            extra_content = msg.pop("extra_content", None)
            if isinstance(extra_content, dict):
                google_ts = (extra_content.get("google") or {}).get("thought_signature")
                if google_ts and msg.get("tool_calls"):
                    pass
                elif google_ts and not msg.get("tool_calls"):
                    msg["extra_content"] = extra_content

            if not msg.get("tool_calls"):
                continue

            for tc in msg["tool_calls"]:
                ts = tc.pop("thought_signature", None)
                if ts:
                    existing = tc.get("extra_content", {})
                    if not isinstance(existing, dict):
                        existing = {}
                    google = existing.get("google", {})
                    if not isinstance(google, dict):
                        google = {}
                    google["thought_signature"] = ts
                    existing["google"] = google
                    tc["extra_content"] = existing

    def openai_upload_file(self, file_tuple, purpose="fine-tune"):
        """Upload a file to OpenAI"""
        response = self.client.files.create(file=file_tuple, purpose=purpose)
        return response

    def openai_create_fine_tuning_job(
        self, training_file_id, model_name, hyperparameters=None
    ):
        """Create an OpenAI fine-tuning job."""
        self.ensure_one()

        hyperparameters = hyperparameters or {}
        hyperparams_cleaned = {
            k: v for k, v in hyperparameters.items() if v is not None
        }

        response = self.client.fine_tuning.jobs.create(
            training_file=training_file_id,
            model=model_name,
            # Pass None if cleaned dict is empty, otherwise pass the dict
            hyperparameters=hyperparams_cleaned if hyperparams_cleaned else None,
        )
        _logger.info(
            f"Fine-tuning job created successfully for provider '{self.name}'. Job ID: {response.id}"
        )
        return response

    def openai_retrieve_training_job(self, job_id):
        """Retrieve an OpenAI fine-tuning job."""
        self.ensure_one()
        response = self.client.fine_tuning.jobs.retrieve(job_id)
        return response

    def openai_cancel_training_job(self, job_id):
        """Cancel an OpenAI fine-tuning job."""
        self.ensure_one()
        response = self.client.fine_tuning.jobs.cancel(job_id)
        return response

    def openai_validate_datasets(self, job):
        """Validate datasets for training"""
        if not job.dataset_ids:
            raise UserError(
                f"Job '{job.name}': Please select at least one dataset before validating."
            )

        for dataset in job.dataset_ids:
            result = dataset.validate_dataset()
            if not result["valid"]:
                raise UserError(
                    f"Validation failed for job '{job.name}':\nDataset '{dataset.name}': {result['message']}"
                )

        return True

    def openai_start_training_job(self, job):
        """Start a training job with the provider."""
        self.ensure_one()

        if not job.dataset_ids:
            raise UserError(f"Job '{self.name}': No datasets linked for preparation.")

        final_combined_bytes = self._openai_get_combined_content_bytes(job)

        if not final_combined_bytes:
            raise UserError(
                f"Job '{job.name}': Combined content from all datasets is empty after processing."
            )

        # Create a filename for the upload (e.g., based on job name or dataset name)
        upload_filename = f"{job.name or 'job'}_combined_datasets.jsonl"

        file_obj = io.BytesIO(final_combined_bytes)
        file_tuple = (upload_filename, file_obj)

        file_upload_response = job.provider_id.upload_file(
            file_tuple, purpose="fine-tune"
        )
        training_file_id = file_upload_response.id

        hyperparameters = job.hyperparameters
        if isinstance(hyperparameters, str):
            try:
                hyperparameters = json.loads(hyperparameters)
            except (json.JSONDecodeError, ValueError):
                hyperparameters = {}
        elif not isinstance(hyperparameters, dict):
            hyperparameters = {}

        training_job_response = job.provider_id.create_fine_tuning_job(
            training_file_id=training_file_id,
            model_name=job.base_model_id.name,
            hyperparameters=hyperparameters,
        )

        return {
            "training_job_id": training_job_response.id,
        }

    @api.model
    def _openai_get_combined_content_bytes(self, job):
        """Get combined content bytes for OpenAI"""
        all_datasets_bytes = []
        dataset_names = []
        for dataset in job.dataset_ids:
            content_bytes = dataset._get_combined_content_bytes()
            if content_bytes:
                all_datasets_bytes.append(content_bytes)
                dataset_names.append(dataset.name)
            else:
                _logger.warning(
                    f"Dataset '{dataset.name}' for job '{job.name}' resulted in empty content, skipping."
                )

        if not all_datasets_bytes:
            raise UserError(
                f"Job '{job.name}': No valid content found in any linked dataset."
            )

        final_combined_bytes = b"".join(all_datasets_bytes)

        if not final_combined_bytes:
            raise UserError(
                f"Job '{self.name}': Combined content from all datasets is empty after processing."
            )

        return final_combined_bytes

    def openai_check_training_job_status(self, job):
        """Check the status of a training job with the provider."""
        self.ensure_one()
        response = job.provider_id.retrieve_training_job(job_id=job.external_job_id)
        state_to_return = OPENAI_TO_ODOO_STATE_MAPPING.get(response.status)
        model_dump = response.model_dump()
        if response.status == "succeeded":
            models_data = job.provider_id.list_models(
                model_id=response.fine_tuned_model
            )
            for model_data in models_data:
                details = model_data.get("details", {})
                name = model_data.get("name") or details.get("id")

                if not name:
                    continue

                # Determine model use and capabilities
                capabilities = details.get("capabilities", ["chat"])
                model_use = job.provider_id._determine_model_use(
                    name, capabilities
                )

                vals = {
                    "name": name,
                    "model_use": model_use,
                    "details": details,
                    "provider_id": job.provider_id.id,
                    "active": True,
                }
                model_exists = self.env["llm.model"].search([("name", "=", name)])
                if not model_exists:
                    result = self.env["llm.model"].create(vals)
                else:
                    result = model_exists

                return {
                    "state": state_to_return,
                    "result_model_id": result.id,
                    "trained_model_name": response.fine_tuned_model,
                    "response": model_dump,
                }

        return {
            "state": state_to_return,
            "response": model_dump,
        }
