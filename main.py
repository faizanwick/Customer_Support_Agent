import os
from pathlib import Path
from typing import TypedDict

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_groq import ChatGroq
from langgraph.graph import StateGraph, END
from pydantic import BaseModel

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
GROQ_MODEL = "openai/gpt-oss-120b"
app = FastAPI(title="Support Agent")

# This is enough for a local demo; conversations disappear when the app stops.
CONVERSATIONS: dict[str, list[dict]] = {}


class ChatState(TypedDict):
    conversation_id: str
    message: str
    history: list[dict]
    route: str
    reply: str


class ChatRequest(BaseModel):
    conversation_id: str
    message: str


class ChatResponse(BaseModel):
    reply: str
    route: str
    escalated: bool


def get_chat_model() -> ChatGroq:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="Groq isn't configured. Add GROQ_API_KEY to your .env file.",
        )
    return ChatGroq(model=GROQ_MODEL, api_key=api_key, temperature=0)


def route_message(state: ChatState) -> ChatState:
    """Choose whether to answer, use a tool, or hand the chat to a person."""
    message = state["message"].lower()
    if "refund" in message or "human" in message:
        state["route"] = "needs_human"
    else:
        result = get_chat_model().invoke(
            [
                SystemMessage(
                    content=(
                        "Classify the customer's message for a support workflow. "
                        "Reply with exactly one label: answerable, needs_tool, or needs_human. "
                        "Use needs_tool for requests that require checking an order or account. "
                        "Use needs_human for sensitive, unresolved, or agent-request cases. "
                        "Otherwise use answerable."
                    )
                ),
                HumanMessage(content=state["message"]),
            ]
        )
        route = str(result.content).strip().lower()
        state["route"] = route if route in {"answerable", "needs_tool", "needs_human"} else "answerable"
    return state


def branch_on_route(state: ChatState) -> str:
    return state["route"]


def call_tool(state: ChatState) -> ChatState:
    """Return a sample order status until an order lookup is wired in."""
    state["reply"] = "[stub] order status: shipped"
    return state


def answer_directly(state: ChatState) -> ChatState:
    """Answer using the recent conversation, without inventing account details."""
    messages = [
        SystemMessage(
            content=(
                "You are a helpful customer support agent. Keep replies clear and concise. "
                "Use the conversation for context, but don't invent order, account, or policy "
                "details. This demo has no connected order lookup; say so if information "
                "requires checking an order."
            )
        )
    ]
    for turn in state["history"]:
        if turn["role"] == "user":
            messages.append(HumanMessage(content=turn["content"]))
        elif turn["role"] == "assistant":
            messages.append(AIMessage(content=turn["content"]))
    messages.append(HumanMessage(content=state["message"]))
    result = get_chat_model().invoke(messages)
    state["reply"] = str(result.content)
    return state


def escalate(state: ChatState) -> ChatState:
    """Let the customer know a person needs to take over."""
    state["reply"] = "I'm connecting you with a human agent who can help with this."
    return state


def build_graph():
    graph = StateGraph(ChatState)
    graph.add_node("route", route_message)
    graph.add_node("answerable", answer_directly)
    graph.add_node("needs_tool", call_tool)
    graph.add_node("needs_human", escalate)

    graph.set_entry_point("route")
    graph.add_conditional_edges(
        "route",
        branch_on_route,
        {
            "answerable": "answerable",
            "needs_tool": "needs_tool",
            "needs_human": "needs_human",
        },
    )
    graph.add_edge("answerable", END)
    graph.add_edge("needs_tool", END)
    graph.add_edge("needs_human", END)

    return graph.compile()


support_graph = build_graph()


@app.get("/chat", response_class=FileResponse)
def chat_page() -> FileResponse:
    return FileResponse(Path(__file__).with_name("chat.html"))


@app.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest) -> ChatResponse:
    history = CONVERSATIONS.setdefault(payload.conversation_id, [])
    history.append({"role": "user", "content": payload.message})

    final_state = support_graph.invoke(
        {
            "conversation_id": payload.conversation_id,
            "message": payload.message,
            "history": history[:-1],
            "route": "",
            "reply": "",
        }
    )

    history.append(
        {
            "role": "assistant",
            "content": final_state["reply"],
            "route": final_state["route"],
        }
    )

    return ChatResponse(
        reply=final_state["reply"],
        route=final_state["route"],
        escalated=final_state["route"] == "needs_human",
    )


@app.get("/")
def health() -> dict:
    return {"status": "ok", "hint": "Open /chat to talk to the assistant, or see /docs"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)