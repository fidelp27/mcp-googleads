"""Register every agency extension on the shared FastMCP server."""

from .analysis_profiles import register_analysis_profile_tools
from .channels_tools import register_channel_tools
from .customer_match_tools import register_customer_match_tools
from .demand_gen_tools import register_demand_gen_tools
from .diagnostics_tools import register_diagnostics_tools
from .experiment_tools import register_experiment_tools
from .governance_tools import register_governance_tools
from .keyword_planner_tools import register_keyword_planner_tools
from .mcc_tools import register_mcc_tools
from .measurement_tools import register_measurement_tools
from .modules import configure_tool_catalog
from .offline_conversions_tools import register_offline_conversions_tools
from .operations_tools import register_operations_tools
from .read_tools import register_read_tools
from .reports import register_report_tools
from .resources import register_resources
from .waste_intelligence import register_waste_intelligence_tools
from .write_tools import register_write_tools


def register_agency_tools(mcp) -> None:
    register_read_tools(mcp)
    register_write_tools(mcp)
    register_report_tools(mcp)
    register_mcc_tools(mcp)
    register_keyword_planner_tools(mcp)
    register_customer_match_tools(mcp)
    register_experiment_tools(mcp)
    register_demand_gen_tools(mcp)
    register_offline_conversions_tools(mcp)
    register_waste_intelligence_tools(mcp)
    register_governance_tools(mcp)
    register_analysis_profile_tools(mcp)
    register_measurement_tools(mcp)
    register_diagnostics_tools(mcp)
    register_channel_tools(mcp)
    register_operations_tools(mcp)
    register_resources(mcp)
    configure_tool_catalog(mcp)
