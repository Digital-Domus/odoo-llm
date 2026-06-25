16.0.1.1.6 (2026-06-25)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Fixed 400 error from Gemini OpenAI-compatible endpoint when using tool calls by
  preserving ``thought_signature`` in ``extra_content.google`` format on tool calls in
  subsequent requests (required by Gemini 2.5+ and 3.x models)
* [IMP] Added proactive Gemini endpoint detection based on ``api_base`` URL to enable
  ``thought_signature`` handling before the first response
* [IMP] Extended ``_extract_thought_signature`` to check ``extra_content.google`` (Gemini
  streaming format) in addition to existing ``google`` and ``model_extra`` paths
* [IMP] Added ``_extract_thought_signature_from_message`` for message-level thought_signature
  extraction (Gemini returns it on delta/message objects for thinking parts)

16.0.1.1.5 (2026-01-07)
~~~~~~~~~~~~~~~~~~~~~~~

* [REM] Removed provider and model data files - users now create providers manually
* [IMP] Provider/model data is now user-owned and survives module uninstall

16.0.1.1.4 (2025-11-17)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Fixed _determine_model_use() call in training job handler to use provider method instead of wizard

16.0.1.1.3 (2025-05-13)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Fine tuning support

16.0.1.1.2 (2025-04-08)
~~~~~~~~~~~~~~~~~~~~~~~

* [IMP] Added workaround for Gemini API compatibility (generates placeholder `tool_call_id` if missing)
* [IMP] Modified message formatting to conditionally include `content` key for Gemini compatibility
* [FIX] Fixed errors when using Gemini API due to missing `tool_call_id`

16.0.1.1.1 (2025-04-03)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Added default model for OpenAI, will work when user adds API key

16.0.1.1.0 (2025-03-06)
~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] Tool support for OpenAI models - Implemented function calling capabilities
* [IMP] Enhanced message handling for tool execution
* [IMP] Added support for processing tool results in chat context

16.0.1.0.0 (2025-01-02)
~~~~~~~~~~~~~~~~~~~~~~~

* [INIT] Initial release of the module
