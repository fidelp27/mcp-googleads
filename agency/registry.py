"""Register every agency extension on the shared FastMCP server."""

from .customer_match_tools import register_customer_match_tools
from .experiment_tools import register_experiment_tools
from .governance_tools import register_governance_tools
from .keyword_planner_tools import register_keyword_planner_tools
from .mcc_tools import register_mcc_tools
from .read_tools import register_read_tools
from .reports import register_report_tools
from .resources import register_resources
from .write_tools import register_write_tools


def register_agency_tools(mcp) -> None:
    register_read_tools(mcp)
    register_write_tools(mcp)
    register_report_tools(mcp)
    register_mcc_tools(mcp)
    register_keyword_planner_tools(mcp)
    register_customer_match_tools(mcp)
    register_experiment_tools(mcp)
    register_governance_tools(mcp)
    register_resources(mcp)
