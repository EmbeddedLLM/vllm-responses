import { readFileSync } from "fs";
import { lookup } from "node:dns/promises";
import { isIP } from "node:net";

type PolicyMode = "allowlist" | "denylist";
type PolicyRuleKind = "host" | "suffix" | "cidr";
type DenialReason =
  | "scheme_not_allowed"
  | "ip_literal_blocked"
  | "special_use_address_blocked"
  | "host_not_allowlisted"
  | "host_denylisted"
  | "cidr_not_allowlisted"
  | "cidr_denylisted";

interface RawPolicyRule {
  kind: PolicyRuleKind;
  value: string;
}

interface RawEgressPolicy {
  mode: PolicyMode;
  allowed_schemes: string[];
  block_ip_literals: boolean;
  block_private_networks: boolean;
  rules: RawPolicyRule[];
}

interface ParsedIp {
  family: 4 | 6;
  value: bigint;
  normalized: string;
}

interface CidrRule {
  family: 4 | 6;
  network: bigint;
  prefix: number;
}

interface EgressPolicy {
  mode: PolicyMode;
  allowedSchemes: Set<string>;
  blockIpLiterals: boolean;
  blockPrivateNetworks: boolean;
  hostRules: Set<string>;
  suffixRules: string[];
  cidrRules: CidrRule[];
}

let activePolicy: EgressPolicy | null = null;
let originalFetch: typeof fetch | null = null;

export function loadEgressPolicy(policyPath: string): EgressPolicy {
  let raw: unknown;
  try {
    raw = JSON.parse(readFileSync(policyPath, "utf8"));
  } catch (error: any) {
    throw new Error(`Failed to load code interpreter egress policy ${JSON.stringify(policyPath)}: ${error?.message || String(error)}`);
  }
  return parsePolicy(raw);
}

export function installEgressPolicy(policyPath: string): void {
  const policy = loadEgressPolicy(policyPath);
  activePolicy = policy;

  if (originalFetch === null) {
    originalFetch = globalThis.fetch.bind(globalThis);
    (globalThis as any).fetch = async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
      const requestUrl = requestUrlFromFetchInput(input);
      await enforceEgressPolicy(requestUrl);
      return originalFetch!(input, init);
    };
  }
}

export function hasActiveEgressPolicy(): boolean {
  return activePolicy !== null;
}

export async function enforceEgressPolicy(rawUrl: string | URL): Promise<void> {
  const policy = activePolicy;
  if (policy === null) {
    return;
  }
  const url = parseRequestUrl(rawUrl);
  const host = normalizeHost(url.hostname);
  const scheme = normalizeScheme(url.protocol);
  const literalIp = parseIp(host);
  const candidateIps = literalIp === null ? await resolveHost(host) : [literalIp];
  evaluatePolicy({ policy, url, host, scheme, literalIp, candidateIps });
}

export function enforceEgressPolicySync(rawUrl: string | URL): void {
  const policy = activePolicy;
  if (policy === null) {
    return;
  }
  const url = parseRequestUrl(rawUrl);
  const host = normalizeHost(url.hostname);
  const scheme = normalizeScheme(url.protocol);
  const literalIp = parseIp(host);
  const candidateIps = literalIp === null && policyRequiresResolvedIps(policy) ? resolveHostSync(host) : literalIp === null ? [] : [literalIp];
  evaluatePolicy({ policy, url, host, scheme, literalIp, candidateIps });
}

function parsePolicy(raw: unknown): EgressPolicy {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new Error("Code interpreter egress policy must be a JSON object.");
  }
  const value = raw as Record<string, unknown>;
  const mode = value.mode;
  if (mode !== "allowlist" && mode !== "denylist") {
    throw new Error("Code interpreter egress policy field `mode` must be `allowlist` or `denylist`.");
  }
  const allowedSchemes = value.allowed_schemes;
  if (!Array.isArray(allowedSchemes) || allowedSchemes.length === 0) {
    throw new Error("Code interpreter egress policy field `allowed_schemes` must be a non-empty array.");
  }
  const schemeSet = new Set<string>();
  for (const scheme of allowedSchemes) {
    if (scheme !== "http" && scheme !== "https") {
      throw new Error("Code interpreter egress policy `allowed_schemes` entries must be `http` or `https`.");
    }
    schemeSet.add(scheme);
  }

  const rules = value.rules;
  if (!Array.isArray(rules)) {
    throw new Error("Code interpreter egress policy field `rules` must be an array.");
  }

  const hostRules = new Set<string>();
  const suffixRules: string[] = [];
  const cidrRules: CidrRule[] = [];
  for (const rule of rules) {
    if (!isRawRule(rule)) {
      throw new Error("Code interpreter egress policy rule entries must include string `kind` and `value` fields.");
    }
    if (rule.kind === "host") {
      hostRules.add(normalizeHost(rule.value));
    } else if (rule.kind === "suffix") {
      const suffix = normalizeHost(rule.value);
      if (!suffix.startsWith(".")) {
        throw new Error("Code interpreter egress policy `suffix` rules must start with `.`.");
      }
      suffixRules.push(suffix);
    } else if (rule.kind === "cidr") {
      cidrRules.push(parseCidr(rule.value));
    }
  }

  return {
    mode,
    allowedSchemes: schemeSet,
    blockIpLiterals: requireBoolean(value.block_ip_literals, "block_ip_literals"),
    blockPrivateNetworks: requireBoolean(value.block_private_networks, "block_private_networks"),
    hostRules,
    suffixRules,
    cidrRules,
  };
}

function isRawRule(value: unknown): value is RawPolicyRule {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const record = value as Record<string, unknown>;
  return (
    (record.kind === "host" || record.kind === "suffix" || record.kind === "cidr") && typeof record.value === "string" && record.value.trim() !== ""
  );
}

function requireBoolean(value: unknown, fieldName: string): boolean {
  if (typeof value !== "boolean") {
    throw new Error(`Code interpreter egress policy field \`${fieldName}\` must be a boolean.`);
  }
  return value;
}

function requestUrlFromFetchInput(input: string | URL | Request): string | URL {
  if (input instanceof URL) {
    return input;
  }
  if (typeof input === "string") {
    return input;
  }
  return input.url;
}

function parseRequestUrl(rawUrl: string | URL): URL {
  try {
    return rawUrl instanceof URL ? rawUrl : new URL(String(rawUrl));
  } catch (error: any) {
    throw new Error(`Invalid code interpreter egress URL: ${error?.message || String(error)}`);
  }
}

function normalizeScheme(protocol: string): string {
  return protocol.toLowerCase().replace(/:$/, "");
}

function normalizeHost(host: string): string {
  let normalized = host.trim().toLowerCase();
  if (normalized.startsWith("[") && normalized.endsWith("]")) {
    normalized = normalized.slice(1, -1);
  }
  return normalized.endsWith(".") ? normalized.slice(0, -1) : normalized;
}

async function resolveHost(host: string): Promise<ParsedIp[]> {
  try {
    const answers = await lookup(host, { all: true, verbatim: true });
    const ips = answers.map((answer) => parseIp(answer.address)).filter((ip): ip is ParsedIp => ip !== null);
    if (ips.length === 0) {
      throw new Error("no A/AAAA answers");
    }
    return ips;
  } catch (error: any) {
    throw new Error(`Failed to resolve code interpreter egress host ${JSON.stringify(host)}: ${error?.message || String(error)}`);
  }
}

function resolveHostSync(host: string): ParsedIp[] {
  const localhost = parseLocalhostAlias(host);
  if (localhost !== null) {
    return [localhost];
  }
  try {
    const proc = Bun.spawnSync(["getent", "ahosts", host], {
      stdout: "pipe",
      stderr: "pipe",
    });
    if (proc.exitCode !== 0) {
      throw new Error(new TextDecoder().decode(proc.stderr).trim() || `getent exited ${proc.exitCode}`);
    }
    const ips = new TextDecoder()
      .decode(proc.stdout)
      .split(/\s+/)
      .map((part) => parseIp(part))
      .filter((ip): ip is ParsedIp => ip !== null);
    const unique = new Map(ips.map((ip) => [`${ip.family}:${ip.normalized}`, ip]));
    if (unique.size === 0) {
      throw new Error("no A/AAAA answers");
    }
    return Array.from(unique.values());
  } catch (error: any) {
    throw new Error(`Failed to resolve code interpreter egress host ${JSON.stringify(host)}: ${error?.message || String(error)}`);
  }
}

function parseLocalhostAlias(host: string): ParsedIp | null {
  if (host === "localhost" || host.endsWith(".localhost")) {
    return parseIp("127.0.0.1");
  }
  return null;
}

function evaluatePolicy(args: {
  policy: EgressPolicy;
  url: URL;
  host: string;
  scheme: string;
  literalIp: ParsedIp | null;
  candidateIps: ParsedIp[];
}): void {
  const { policy, url, host, scheme, literalIp, candidateIps } = args;
  if (!policy.allowedSchemes.has(scheme)) {
    deny("scheme_not_allowed", host, scheme);
  }
  if (literalIp !== null && policy.blockIpLiterals) {
    deny("ip_literal_blocked", host, scheme);
  }
  if (policy.blockPrivateNetworks) {
    const specialUseIp = candidateIps.find(isSpecialUseAddress);
    if (specialUseIp !== undefined) {
      deny("special_use_address_blocked", host, scheme, `ip=${specialUseIp.normalized}`);
    }
  }

  const hostMatched = matchesHostRule(policy, host);
  if (policy.mode === "allowlist") {
    if (hostMatched) {
      return;
    }
    if (candidateIps.length > 0 && policy.cidrRules.length > 0 && candidateIps.every((ip) => matchesAnyCidr(policy.cidrRules, ip))) {
      return;
    }
    deny(policy.cidrRules.length > 0 ? "cidr_not_allowlisted" : "host_not_allowlisted", host, scheme, `url=${url.href}`);
  }

  if (hostMatched) {
    deny("host_denylisted", host, scheme);
  }
  if (candidateIps.some((ip) => matchesAnyCidr(policy.cidrRules, ip))) {
    deny("cidr_denylisted", host, scheme);
  }
}

function policyRequiresResolvedIps(policy: EgressPolicy): boolean {
  return policy.blockPrivateNetworks || policy.cidrRules.length > 0;
}

function matchesHostRule(policy: EgressPolicy, host: string): boolean {
  return policy.hostRules.has(host) || policy.suffixRules.some((suffix) => host.endsWith(suffix));
}

function deny(reason: DenialReason, host: string, scheme: string, detail?: string): never {
  const suffix = detail === undefined ? "" : ` ${detail}`;
  throw new Error(`Code interpreter egress denied: host=${host} scheme=${scheme} reason=${reason}${suffix}`);
}

function parseCidr(value: string): CidrRule {
  const [address, prefixString, extra] = value.split("/");
  if (!address || !prefixString || extra !== undefined) {
    throw new Error(`Code interpreter egress policy CIDR rule is invalid: ${value}`);
  }
  const ip = parseIp(address);
  if (ip === null) {
    throw new Error(`Code interpreter egress policy CIDR address is invalid: ${value}`);
  }
  const prefix = Number(prefixString);
  const width = ip.family === 4 ? 32 : 128;
  if (!Number.isInteger(prefix) || prefix < 0 || prefix > width) {
    throw new Error(`Code interpreter egress policy CIDR prefix is invalid: ${value}`);
  }
  return {
    family: ip.family,
    network: normalizeNetwork(ip.value, prefix, width),
    prefix,
  };
}

function matchesAnyCidr(rules: CidrRule[], ip: ParsedIp): boolean {
  return rules.some((rule) => {
    if (rule.family !== ip.family) {
      return false;
    }
    const width = ip.family === 4 ? 32 : 128;
    return normalizeNetwork(ip.value, rule.prefix, width) === rule.network;
  });
}

function normalizeNetwork(value: bigint, prefix: number, width: number): bigint {
  const hostBits = BigInt(width - prefix);
  return hostBits === 0n ? value : (value >> hostBits) << hostBits;
}

function isSpecialUseAddress(ip: ParsedIp): boolean {
  const ranges = ip.family === 4 ? SPECIAL_IPV4_RANGES : SPECIAL_IPV6_RANGES;
  return matchesAnyCidr(ranges, ip);
}

function parseIp(raw: string): ParsedIp | null {
  const normalized = normalizeHost(raw);
  const family = isIP(normalized);
  if (family === 4) {
    const value = parseIpv4(normalized);
    return value === null ? null : { family, value, normalized };
  }
  if (family === 6) {
    const value = parseIpv6(normalized);
    return value === null ? null : { family, value, normalized };
  }
  return null;
}

function parseIpv4(value: string): bigint | null {
  const parts = value.split(".");
  if (parts.length !== 4) {
    return null;
  }
  let result = 0n;
  for (const part of parts) {
    if (!/^\d+$/.test(part)) {
      return null;
    }
    const octet = Number(part);
    if (!Number.isInteger(octet) || octet < 0 || octet > 255) {
      return null;
    }
    result = (result << 8n) + BigInt(octet);
  }
  return result;
}

function parseIpv6(value: string): bigint | null {
  const withoutZone = value.split("%", 1)[0] || "";
  if (!withoutZone.includes(":")) {
    return null;
  }
  const doubleColonParts = withoutZone.split("::");
  if (doubleColonParts.length > 2) {
    return null;
  }

  const left = hextetsFromIpv6Part(doubleColonParts[0] || "");
  const right = hextetsFromIpv6Part(doubleColonParts[1] || "");
  if (left === null || right === null) {
    return null;
  }
  const missing = 8 - left.length - right.length;
  if (doubleColonParts.length === 1 && missing !== 0) {
    return null;
  }
  if (doubleColonParts.length === 2 && missing < 1) {
    return null;
  }
  const hextets = [...left, ...Array(Math.max(missing, 0)).fill(0), ...right];
  if (hextets.length !== 8) {
    return null;
  }
  return hextets.reduce((acc, hextet) => (acc << 16n) + BigInt(hextet), 0n);
}

function hextetsFromIpv6Part(part: string): number[] | null {
  if (part === "") {
    return [];
  }
  const rawHextets = part.split(":");
  const hextets: number[] = [];
  for (const rawHextet of rawHextets) {
    if (rawHextet.includes(".")) {
      const ipv4 = parseIpv4(rawHextet);
      if (ipv4 === null) {
        return null;
      }
      hextets.push(Number((ipv4 >> 16n) & 0xffffn), Number(ipv4 & 0xffffn));
      continue;
    }
    if (!/^[0-9a-fA-F]{1,4}$/.test(rawHextet)) {
      return null;
    }
    hextets.push(parseInt(rawHextet, 16));
  }
  return hextets;
}

const SPECIAL_IPV4_RANGES: CidrRule[] = [
  parseCidr("0.0.0.0/8"),
  parseCidr("10.0.0.0/8"),
  parseCidr("100.64.0.0/10"),
  parseCidr("127.0.0.0/8"),
  parseCidr("169.254.0.0/16"),
  parseCidr("172.16.0.0/12"),
  parseCidr("192.0.0.0/24"),
  parseCidr("192.168.0.0/16"),
  parseCidr("198.18.0.0/15"),
  parseCidr("224.0.0.0/4"),
  parseCidr("240.0.0.0/4"),
];

const SPECIAL_IPV6_RANGES: CidrRule[] = [
  parseCidr("::/128"),
  parseCidr("::1/128"),
  parseCidr("fc00::/7"),
  parseCidr("fe80::/10"),
  parseCidr("ff00::/8"),
  parseCidr("2001:db8::/32"),
];
