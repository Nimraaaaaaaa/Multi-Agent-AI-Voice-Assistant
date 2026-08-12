"""
graph.py
All agents are connected here using LangGraph's StateGraph.

Flow:
    START -> router ->
        (email | app_launcher | whatsapp | vision | file | general)
        -> END
"""

from langgraph.graph import StateGraph, END

from backend.core.state import JarvisState

from backend.agents.router_agent import router_node
from backend.agents.email_agent import email_node
from backend.agents.app_launcher_agent import app_launcher_node
from backend.agents.general_agent import general_node
from backend.agents.whatsapp_agent import whatsapp_node
from backend.agents.vision_agent import vision_agent_node
from backend.agents.file_agent import file_agent_node


def _decide_next(state: JarvisState) -> str:
    return state.get("route") or "general"


def build_graph():
    graph = StateGraph(JarvisState)

    graph.add_node("router", router_node)
    graph.add_node("email", email_node)
    graph.add_node("app_launcher", app_launcher_node)
    graph.add_node("whatsapp", whatsapp_node)
    graph.add_node("vision", vision_agent_node)
    graph.add_node("file", file_agent_node)
    graph.add_node("general", general_node)

    graph.set_entry_point("router")

    graph.add_conditional_edges(
        "router",
        _decide_next,
        {
            "email": "email",
            "app_launcher": "app_launcher",
            "whatsapp": "whatsapp",
            "vision": "vision",
            "file": "file",
            "general": "general",
        },
    )

    graph.add_edge("email", END)
    graph.add_edge("app_launcher", END)
    graph.add_edge("whatsapp", END)
    graph.add_edge("vision", END)
    graph.add_edge("file", END)
    graph.add_edge("general", END)

    return graph.compile()


jarvis_graph = build_graph()