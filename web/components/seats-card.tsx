"use client";

/**
 * Seats on /admin/health: how many people this Celmis holds, against how many
 * it may — `GET /api/admin/seats` (global admins; src/api/routers/seats.py).
 *
 * Without a licence an installation holds five people, five of them through
 * single sign-on (a line the card shows only in a build that has single
 * sign-on); a licence lifts both. The card says the numbers and, without
 * a licence, the rule. When the installation is at its limit it says what that
 * means: nobody who has an account is switched off, a NEW person cannot be
 * added. It never lists who holds a seat.
 */
import { UsersIcon } from "lucide-react";
import { useQuery } from "@tanstack/react-query";

import { Badge } from "@/components/ui/badge";
import { Callout } from "@/components/ui/callout";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { anyFull, anyOver, isOver, type SeatCount, type SeatsState } from "@/lib/seats";
import { useToken } from "@/lib/use-token";

export const SEATS_KEY = ["admin-seats"] as const;

function CountRow({ label, count }: { label: string; count: SeatCount }) {
  const t = useT();
  return (
    <>
      <dt className="text-[var(--color-muted-foreground)]">{label}</dt>
      <dd
        className={isOver(count) ? "font-semibold text-red-600" : undefined}
        data-testid="seat-count"
      >
        {count.limit === null
          ? t("seats.usedUnlimited", { used: count.used })
          : t("seats.usedOfLimit", { used: count.used, limit: count.limit })}
      </dd>
    </>
  );
}

export function SeatsCard() {
  const t = useT();
  const token = useToken();
  const q = useQuery({
    queryKey: SEATS_KEY,
    queryFn: () => api<SeatsState>("/api/admin/seats", { token }),
    enabled: !!token,
    retry: false,
    refetchInterval: 60_000,
  });
  const s = q.data;

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-base">
          <UsersIcon className="h-4 w-4" aria-hidden />
          {t("seats.title")}
          {s && (
            <Badge variant="outline">
              {s.licensed ? t("edition.enterprise") : t("edition.community")}
            </Badge>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {q.isError && (
          <p className="text-xs text-[var(--color-muted-foreground)]">{t("seats.loadFailed")}</p>
        )}
        {s && (
          <>
            <dl className="grid gap-x-6 gap-y-1 sm:grid-cols-[max-content_1fr]">
              <CountRow label={t("seats.seatsLabel")} count={s.seats} />
              {s.sso_seats && <CountRow label={t("seats.ssoLabel")} count={s.sso_seats} />}
            </dl>
            {!s.licensed && (
              <p className="text-xs text-[var(--color-muted-foreground)]">
                {t("seats.communityNote", { seats: s.free_seats })}
              </p>
            )}
            {anyOver(s) ? (
              <Callout tone="warning">{t("seats.overNote")}</Callout>
            ) : anyFull(s) ? (
              <Callout tone="info">{t("seats.fullNote")}</Callout>
            ) : null}
          </>
        )}
      </CardContent>
    </Card>
  );
}
