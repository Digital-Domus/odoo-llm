# Changelog - LLM Assistant Module

All notable changes to the llm_assistant module will be documented in this file.

## [16.0.1.6.0] - 2026-06-26

### Added
- **Partner for Chat**: New `partner_id` field linking to a partner record
  - When set, the partner's name and avatar are used in AI chat messages
  - Provides a cleaner approach using Odoo's existing partner system

### Changed
- **`_handle_streaming_response`**: Now uses `partner_id` as `author_id` when available
- **`_handle_non_streaming_response`**: Now uses `partner_id` as `author_id` when available
- **`_execute_tool_call`**: Now uses `partner_id` as `author_id` for tool messages

## [16.0.1.5.0] - 2025-07-13

### Added
- **Assistant Code System**: New `code` field for unique assistant identification
  - Unique constraint ensuring assistant codes are globally unique
  - Index on code field for performance optimization
  - Enables programmatic assistant discovery replacing category-based lookups

- **Model Association**: New `res_model` field for linking assistants to specific Odoo models
  - Supports model-specific assistant configurations (e.g., 'fleek.character')
  - Enables filtered assistant discovery based on target model

- **Default Assistant System**: New `is_default` boolean field
  - Marks assistants as defaults for automatic thread creation
  - Combined with `res_model` and `is_public` for granular access control
  - Enables dynamic thread provisioning based on user permissions

- **Assistant Discovery Method**: New `get_assistant_by_code(code)` class method
  - Centralized method for finding assistants by unique code
  - Replaces complex category-based search patterns
  - Used by thread management systems for consistent assistant lookup

### Changed
- **Database Migration**: Version 16.0.1.5.0 migration script
  - Automatically generates dotted codes from existing category hierarchy
  - Converts parent.child.grandchild category structures to assistant codes
  - Handles missing codes with sanitized naming and duplicate resolution

### Technical Details
- **SQL Constraints**: Added `unique_code` constraint for data integrity
- **Performance**: Code field indexed for fast lookups
- **Backward Compatibility**: Migration preserves existing assistant functionality while adding new capabilities
- **Integration**: Seamlessly integrates with existing prompt template and tool systems