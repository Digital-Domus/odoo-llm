import json
import logging
from typing import Any, Union

from odoo import api, models
from odoo.exceptions import AccessError

_logger = logging.getLogger(__name__)


class LLMToolRecordRetriever(models.Model):
    _inherit = "llm.tool"

    @api.model
    def _get_available_implementations(self):
        implementations = super()._get_available_implementations()
        return implementations + [("odoo_record_retriever", "Odoo Record Retriever")]

    def odoo_record_retriever_execute(
        self,
        model: str,
        domain: list[list[Union[str, int, bool, float, None]]] = [],  # noqa: B006
        fields: list[str] = [],  # noqa: B006
        limit: int = 100,
    ) -> dict[str, Any]:
        """
        Execute the Odoo Record Retriever tool

        Parameters:
            model: The Odoo model to retrieve records from
            domain: Domain to filter records (list of lists/tuples like ['field', 'op', 'value'])
            fields: List of field names to retrieve
            limit: Maximum number of records to retrieve
        """
        _logger.info(
            f"Executing Odoo Record Retriever with: model={model}, domain={domain}, fields={fields}, limit={limit}"
        )

        # Validate that the requested model exists before accessing it
        if not model:
            return {"error": "No model name was provided."}
        if model not in self.env:
            return {
                "error": (
                    f"Model '{model}' does not exist in the Odoo environment. "
                    "Please verify the technical model name."
                )
            }

        model_obj = self.env[model]

        # Filter out unknown field names early so we can report them explicitly
        if fields:
            invalid_fields = [
                f for f in fields if f not in model_obj._fields and f != "id"
            ]
            if invalid_fields:
                valid_fields = sorted(model_obj._fields.keys())
                return {
                    "error": (
                        f"Invalid field(s) requested for model '{model}': {invalid_fields}. "
                        f"Valid fields include: {valid_fields}"
                    )
                }

        try:
            # Using search_read for efficiency
            if fields:
                result = model_obj.search_read(
                    domain=domain, fields=fields, limit=limit
                )
            else:
                records = model_obj.search(domain=domain, limit=limit)
                result = records.read()
        except AccessError as e:
            return {
                "error": (
                    f"Access denied to model '{model}' for the current user: {e}"
                )
            }
        except (ValueError, KeyError) as e:
            return {
                "error": (
                    f"Invalid domain or query for model '{model}': {e}. "
                    "Domain must be a list of [field, operator, value] tuples."
                )
            }

        # Convert to serializable format
        return json.loads(json.dumps(result, default=str))
