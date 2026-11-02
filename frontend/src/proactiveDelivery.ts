export type ProactiveMessage = { id: string; content: string; status: "ready" | "delivered" | "read" };
export type ConversationSnapshot = { id: string; messages?: { id: string; role: string; content: string }[] };
export type ApiRequest = <T>(path: string, options?: RequestInit) => Promise<T>;

/** Retry the same durable ID, then replace local history with server history. */
export async function confirmDeliveryAndSync<TMessage extends ProactiveMessage, TConversation extends ConversationSnapshot>(
  message: TMessage,
  conversationId: string,
  contactPath: string,
  request: ApiRequest,
): Promise<{ delivered: TMessage; conversation: TConversation }> {
  const delivered = await request<TMessage>(`${contactPath}/messages/${message.id}/delivered`, {
    method: "POST", body: JSON.stringify({ conversation_id: conversationId }),
  });
  const conversation = await request<TConversation>(`/conversations/${conversationId}`);
  return { delivered, conversation };
}
