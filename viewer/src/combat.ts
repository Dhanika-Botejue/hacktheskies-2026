// Link to the shooting server (combat/server.py, proxied at /combat/): hit points, kills, the
// match clock and every shot. Its players are numbered by the AprilTag on their chest; the game
// server's players carry the same number (GamePlayer.tag), which is how the two are joined.

export type Team = "red" | "blue";

export interface Fighter {
  player_id: number;
  team: Team;
  alive: boolean;
  hp: number;
  max_hp: number;
  kills: number;
  deaths: number;
  connected: boolean;
  seconds_to_respawn: number;
}

export interface Scores {
  red_kills: number;
  blue_kills: number;
  red_deaths: number;
  blue_deaths: number;
  winner: Team | "tie";
}

export interface ShotEvent {
  shooter_id: number;
  target_id: number;
  hp: number;
  max_hp: number;
  killed: boolean;
}

export const TEAM_COLORS: Record<Team, string> = { red: "#ff4d5e", blue: "#4da3ff" };

export class CombatLink {
  connected = false;
  active = false;
  elapsed = 0;
  scores: Scores | null = null;
  fighters = new Map<number, Fighter>();
  onChange: () => void = () => {};
  onShot: (shot: ShotEvent) => void = () => {};
  onMatch: (started: boolean, scores: Scores | null) => void = () => {};
  onStatus: (connected: boolean) => void = () => {};
  private ws: WebSocket | null = null;
  private delay = 500;

  constructor() {
    this.open();
  }

  private open() {
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/combat/ws/host`);
    this.ws = ws;
    ws.onopen = () => {
      this.delay = 500;
      this.connected = true;
      this.onStatus(true);
    };
    ws.onmessage = (e) => this.handle(JSON.parse(e.data));
    ws.onclose = () => {
      if (this.connected) {
        this.connected = false;
        this.fighters.clear();
        this.onStatus(false);
        this.onChange();
      }
      setTimeout(() => this.open(), this.delay);
      this.delay = Math.min(this.delay * 2, 5000);
    };
  }

  private handle(msg: { type: string; [k: string]: unknown }) {
    if (msg.type === "state_update") {
      this.fighters = new Map(Object.values(msg.players as Record<string, Fighter>).map((f) => [f.player_id, f]));
      this.scores = msg.scores as Scores;
      this.active = msg.game_active as boolean;
      this.elapsed = msg.elapsed as number;
      this.onChange();
    } else if (msg.type === "shot_event") {
      this.onShot(msg as unknown as ShotEvent);
    } else if (msg.type === "game_started") {
      this.active = true;
      this.onMatch(true, null);
      this.onChange();
    } else if (msg.type === "game_over") {
      this.active = false;
      this.scores = msg.scores as Scores;
      this.onMatch(false, this.scores);
      this.onChange();
    }
  }

  private send(msg: unknown) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg));
  }

  start() {
    this.send({ type: "start_game" });
  }

  stop() {
    this.send({ type: "stop_game" });
  }

  fighter(tag: number | null | undefined) {
    return tag == null ? null : (this.fighters.get(tag) ?? null);
  }
}

export function hearts(f: Fighter) {
  return "♥".repeat(Math.max(0, f.hp)) + "♡".repeat(Math.max(0, f.max_hp - f.hp));
}
