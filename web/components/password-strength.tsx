"use client";

/**
 * Password strength meter — mirrors `src/users/password_policy.py` so the UI
 * never claims a password is fine that the API will reject (and vice versa).
 * It is a hint, not a control: the server is the authority.
 */

import { useT } from "@/lib/i18n";

export const PASSWORD_MIN_LENGTH = 10;

const COMMON = [
  "password", "passwd", "qwerty", "asdfgh", "zxcvbn", "111111", "123456",
  "12345678", "123456789", "1234567890", "letmein", "welcome", "admin",
  "iloveyou", "monkey", "dragon", "sunshine", "princess", "football",
  "abc123", "changeme", "secret", "master", "login", "celmis",
];

function classCount(pwd: string): number {
  return [/[A-Z]/, /[a-z]/, /\d/, /[^A-Za-z0-9]/].filter((rx) => rx.test(pwd)).length;
}

/** Same rules as the backend validator — returns i18n keys. */
export function passwordProblemKeys(pwd: string, email = ""): string[] {
  const out: string[] = [];
  if (pwd.length < PASSWORD_MIN_LENGTH) out.push("pw.tooShort");
  if (classCount(pwd) < 3) out.push("pw.needClasses");
  const lower = pwd.toLowerCase();
  if (COMMON.some((c) => lower.includes(c))) out.push("pw.common");
  if (pwd && pwd.trim() !== pwd) out.push("pw.whitespace");
  if (pwd && new Set(pwd).size <= 3) out.push("pw.repetitive");
  const local = (email.split("@")[0] || "").toLowerCase();
  if (local.length >= 4 && lower.includes(local)) out.push("pw.containsEmail");
  return out;
}

export function passwordScore(pwd: string): number {
  if (pwd.length < PASSWORD_MIN_LENGTH) return 0;
  let score = 1;
  const classes = classCount(pwd);
  if (classes >= 3) score += 1;
  if (pwd.length >= 14) score += 1;
  if (pwd.length >= 18 && classes === 4) score += 1;
  if (COMMON.some((c) => pwd.toLowerCase().includes(c))) score = Math.min(score, 1);
  return Math.max(0, Math.min(4, score));
}

const BAR = ["bg-red-500", "bg-red-500", "bg-amber-500", "bg-emerald-500", "bg-emerald-600"];
const LABEL_KEY = ["pw.veryWeak", "pw.weak", "pw.fair", "pw.good", "pw.strong"];

export function PasswordStrength({ value, email = "" }: { value: string; email?: string }) {
  const t = useT();
  if (!value) return null;
  const score = passwordScore(value);
  const problems = passwordProblemKeys(value, email);
  return (
    <div className="mt-1.5 space-y-1">
      <div className="flex gap-1">
        {[0, 1, 2, 3].map((i) => (
          <div
            key={i}
            className={`h-1 flex-1 rounded-full ${i < score ? BAR[score] : "bg-[var(--color-muted)]"}`}
          />
        ))}
      </div>
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span className="text-[var(--color-muted-foreground)]">{t(LABEL_KEY[score])}</span>
        {problems.length > 0 && (
          <span className="text-right text-amber-600 dark:text-amber-400">
            {t(problems[0])}
          </span>
        )}
      </div>
    </div>
  );
}
