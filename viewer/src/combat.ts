// Link to the shooting server (combat/server.py, proxied at /combat/): hit points, kills, every
// shot, and the match it runs: who owns the flag, each team's flag time, respawns at the bases.
// Its players are numbered (the number they were scanned as at the scan station, or printed on
// their tag in tags mode); the game server's players carry the same number (GamePlayer.tag), which
// is how the two are joined.

export type Team = "red" | "blue";
/** How a downed player comes back: by walking into their base, or after a timer (no base / not tracked). */
export type RespawnMode = "base" | "timer";

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
  respawn_mode: RespawnMode;
  /** The shooting server knows where they are (from the game server). */
  tracked?: boolean;
  /** Clothes mode: they've been scanned at the scan station, so they can be recognised. */
  scanned?: boolean;
}

/** How players are told apart: their clothes (scanned before the game) or printed tags. */
export type IdMode = "clothes" | "tags";

export interface Scores {
  red_kills: number;
  blue_kills: number;
  red_deaths: number;
  blue_deaths: number;
  /** A flag on the map: the flag times decide, kills only break a tie. */
  flag: boolean;
  red_flag_s: number;
  blue_flag_s: number;
  flag_owner: Team | null;
  win_s: number;
  /** Match length (null: no time limit) and what's left of it: most kills at the end wins. */
  match_s: number | null;
  time_left: number | null;
  winner: Team | "tie";
  /** Why the match ended: time ran out, a team's flag time reached win_s, or the host ended it. */
  reason: "time" | "flag" | "host" | null;
}

export interface Zone {
  x: number;
  y: number;
  r: number;
}

export interface FlagState extends Zone {
  owner: Team | null;
  /** The team outnumbering the owner in the circle, about to take it (progress 0–1). */
  challenger: Team | null;
  progress: number;
  inside: Record<Team, number>;
}

/** The match as the shooting server runs it (game map metres, like the game server's). */
export interface Objectives {
  bases: Partial<Record<Team, Zone>>;
  flag: FlagState | null;
  held: Record<Team, number>;
  win_s: number;
  capture_s: number;
  /** The shooting server hears positions from the game server: needed for bases and the flag. */
  tracking: boolean;
}

export interface FlagTaken {
  team: Team;
  from: Team | null;
}

export interface Respawned {
  player_id: number;
  how: "base" | "timer" | "host";
}

export interface ShotEvent {
  shooter_id: number;
  target_id: number;
  hp: number;
  max_hp: number;
  killed: boolean;
}

export const TEAM_COLORS: Record<Team, string> = { red: "#ff4d5e", blue: "#4da3ff" };
export const TEAM_NAMES: Record<Team, string> = { red: "Red", blue: "Blue" };

export const clock = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

export class CombatLink {
  connected = false;
  active = false;
  elapsed = 0;
  scores: Scores | null = null;
  objectives: Objectives | null = null;
  fighters = new Map<number, Fighter>();
  idMode: IdMode = "clothes";
  onChange: () => void = () => {};
  onShot: (shot: ShotEvent) => void = () => {};
  onMatch: (started: boolean, scores: Scores | null) => void = () => {};
  onFlag: (e: FlagTaken) => void = () => {};
  onRespawn: (e: Respawned) => void = () => {};
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
        this.objectives = null;
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
      this.objectives = msg.objectives as Objectives;
      this.active = msg.game_active as boolean;
      this.elapsed = msg.elapsed as number;
      if (msg.id_mode === "tags" || msg.id_mode === "clothes") this.idMode = msg.id_mode;
      this.onChange();
    } else if (msg.type === "shot_event") {
      this.onShot(msg as unknown as ShotEvent);
    } else if (msg.type === "flag_taken") {
      this.onFlag(msg as unknown as FlagTaken);
    } else if (msg.type === "respawned") {
      this.onRespawn(msg as unknown as Respawned);
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

  /** winS: flag time that wins the match. */
  /** matchS: match length; the team with more kills when it runs out wins. */
  start(matchS: number) {
    this.send({ type: "start_game", match_s: matchS });
  }

  /** Bring a downed player back by hand (when their tracking went wrong). */
  respawn(tag: number) {
    this.send({ type: "respawn_player", player_id: tag });
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

/** "♥♥♡", or what a downed player is waiting for. */
export function healthText(f: Fighter | null) {
  if (!f) return "–";
  if (f.alive) return hearts(f);
  return f.respawn_mode === "base" ? "down · to base" : `down ${Math.ceil(f.seconds_to_respawn)}s`;
}
