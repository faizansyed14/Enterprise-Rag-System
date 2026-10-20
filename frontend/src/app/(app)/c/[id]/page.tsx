"use client";

import { useParams } from "next/navigation";

import { ChatView } from "@/components/chat/chat-view";

export default function ConversationPage() {
  const { id } = useParams<{ id: string }>();
  return <ChatView key={id} conversationId={id} />;
}
