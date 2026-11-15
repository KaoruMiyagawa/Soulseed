import { useEffect, useLayoutEffect, useRef, type ReactNode } from "react";

export type SendMotion = { key: string; origin: DOMRect };

function animateSend(bubble: HTMLElement, origin: DOMRect) {
  const destination = bubble.getBoundingClientRect();
  if (!destination.width || !destination.height || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const duration = 290;
  if (destination.height > 180) {
    const slide = bubble.animate(
      [{ transform: "translateY(18px) scale(.985)", opacity: .9 }, { transform: "none", opacity: 1 }],
      { duration: 220, easing: "cubic-bezier(.2,.75,.25,1)" },
    );
    return () => slide.cancel();
  }
  // The visual copy travels above the composer's clipping edge. The message itself keeps its layout slot and key.
  const overlay = bubble.cloneNode(true) as HTMLElement;
  overlay.className = "chat-flight";
  overlay.setAttribute("aria-hidden", "true");
  const style = getComputedStyle(bubble);
  for (const property of ["font", "letter-spacing", "color", "border", "border-radius", "background-color", "padding"]) {
    overlay.style.setProperty(property, style.getPropertyValue(property));
  }
  Object.assign(overlay.style, {
    top: `${destination.top}px`, left: `${destination.left}px`,
    width: `${destination.width}px`, height: `${destination.height}px`,
  });
  document.body.append(overlay);
  const hidden = bubble.animate([{ opacity: 0 }, { opacity: 0 }], { duration, fill: "both" });
  const flight = overlay.animate([
    { transform: `translate(${origin.right - 20 - destination.right}px, ${origin.top + 10 - destination.top}px) scale(.94)`,
      opacity: .91, easing: "cubic-bezier(.2,.76,.23,1)" },
    { transform: "translateY(-2px) scale(1.006)", opacity: 1, offset: .82, easing: "ease-out" },
    { transform: "none", opacity: 1 },
  ], { duration, fill: "both" });
  const finish = () => {
    flight.cancel();
    hidden.cancel();
    overlay.remove();
  };
  void flight.finished.then(finish, finish);
  return finish;
}

export function ChatTranscript({ conversationId, messageCount, motion, children }: {
  conversationId?: string; messageCount: number; motion?: SendMotion; children: ReactNode;
}) {
  const list = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const previousConversation = useRef(conversationId);
  const animated = useRef<string | undefined>(undefined);
  const moving = useRef<{ stop: () => void; top: number } | undefined>(undefined);

  useLayoutEffect(() => {
    const node = list.current;
    if (!node) return;
    const bubble = motion && motion.key !== animated.current
      ? node.querySelector<HTMLElement>(`[data-chat-key="${CSS.escape(motion.key)}"] .chat-bubble`)
      : null;
    if (previousConversation.current !== conversationId || following.current || bubble) {
      node.scrollTop = node.scrollHeight;
      following.current = true;
    }
    if (bubble && motion) {
      animated.current = motion.key;
      moving.current?.stop();
      const stop = animateSend(bubble, motion.origin);
      moving.current = stop ? { stop, top: node.scrollTop } : undefined;
    }
    previousConversation.current = conversationId;
  }, [conversationId, messageCount, motion]);

  useEffect(() => {
    const stop = () => moving.current?.stop();
    window.addEventListener("resize", stop);
    return () => {
      window.removeEventListener("resize", stop);
      stop();
    };
  }, []);

  return <div className="messages" role="region" aria-label="聊天记录" tabIndex={0} ref={list}
    onScroll={(event) => {
      const node = event.currentTarget;
      if (moving.current && Math.abs(moving.current.top - node.scrollTop) > 2) {
        moving.current.stop();
        moving.current = undefined;
      }
      following.current = node.scrollHeight - node.scrollTop - node.clientHeight < 80;
    }}>
    {children}
  </div>;
}
