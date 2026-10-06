import { execFileSync } from "node:child_process";
import path from "node:path";

function parseRemote(remote) {
  const value = remote.trim().replace(/\.git$/i, "");
  const https = value.match(/^https?:\/\/([^/]+)\/([^/]+)\/([^/]+)$/i);
  if (https) return { host: https[1].toLowerCase(), full_name: `${https[2]}/${https[3]}`.toLowerCase() };
  const ssh = value.match(/^(?:[^@]+@)?([^:]+):([^/]+)\/([^/]+)$/);
  if (ssh) return { host: ssh[1].toLowerCase(), full_name: `${ssh[2]}/${ssh[3]}`.toLowerCase() };
  return null;
}

export function inspectCheckout(root) {
  const absoluteRoot = path.resolve(root);
  try {
    const top = execFileSync("git", ["-C", absoluteRoot, "rev-parse", "--show-toplevel"], { encoding: "utf8" }).trim();
    const remoteNames = execFileSync("git", ["-C", absoluteRoot, "remote"], { encoding: "utf8" }).trim().split(/\r?\n/).filter(Boolean);
    const remotes = remoteNames.map((name) => {
      const url = execFileSync("git", ["-C", absoluteRoot, "remote", "get-url", name], { encoding: "utf8" }).trim();
      return { name, url, ...parseRemote(url) };
    }).filter((remote) => remote.host && remote.full_name);
    return { root: top, remotes };
  } catch (error) {
    throw new Error(`target path is not a Git checkout with a readable remote: ${error.message}`);
  }
}

export function checkoutMatchesRepo(checkout, repo, host = "github.com") {
  const target = repo.toLowerCase();
  return checkout.remotes.some((remote) => remote.full_name === target && remote.host === host.toLowerCase());
}
