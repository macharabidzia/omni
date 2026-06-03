import type { BrowserOutboundEvent, GatewayInboundEvent } from "./events";

type RealtimeClientOptions = {
  url: string;
  onEvent: (event: GatewayInboundEvent) => void;
  onClose: () => void;
  onError: (message: string) => void;
};

export class RealtimeClient {
  private socket: WebSocket | null = null;

  constructor(private readonly options: RealtimeClientOptions) {}

  connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      this.socket = new WebSocket(this.options.url);
      this.socket.onopen = () => resolve();
      this.socket.onclose = () => this.options.onClose();
      this.socket.onerror = () => {
        const message = "Realtime websocket error.";
        this.options.onError(message);
        reject(new Error(message));
      };
      this.socket.onmessage = (message) => {
        const payload = JSON.parse(message.data) as GatewayInboundEvent;
        this.options.onEvent(payload);
      };
    });
  }

  send(event: BrowserOutboundEvent): void {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      throw new Error("Realtime websocket is not connected.");
    }
    this.socket.send(JSON.stringify(event));
  }

  close(): void {
    this.socket?.close();
    this.socket = null;
  }
}

