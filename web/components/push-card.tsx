"use client";
import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { BellIcon, BellOffIcon, Loader2Icon } from "lucide-react";
import { toast } from "sonner";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { pushApi } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import {
  currentSubscription, disablePush, enablePush, isStandalone, localState,
  type PushState,
} from "@/lib/push";

/**
 * Turning on "tell me when it finishes".
 *
 * The card is mostly about the refusals. A browser can decline in four ways
 * that need four different actions from the person reading this, and the one
 * they will actually hit — iOS refusing a plain Safari tab — is impossible to
 * guess from a generic error.
 */
export function PushCard() {
  const t = useT();
  const token = useToken();
  const [state, setState] = useState<PushState | null>(null);

  const cfg = useQuery({
    queryKey: ["push-config"],
    queryFn: () => pushApi.config(token!),
    enabled: Boolean(token),
  });

  useEffect(() => {
    let alive = true;
    void (async () => {
      const local = localState();
      if (local !== "ready") { if (alive) setState(local); return; }
      const sub = await currentSubscription();
      if (alive) setState(sub ? "subscribed" : "ready");
    })();
    return () => { alive = false; };
  }, [cfg.data?.devices]);

  const enable = useMutation({
    mutationFn: async () => {
      await enablePush(cfg.data!.public_key, (body) => pushApi.subscribe(token!, body));
    },
    onSuccess: () => { setState("subscribed"); void cfg.refetch(); toast.success(t("push.enabled")); },
    onError: (e) => {
      const code = (e as Error).message as PushState;
      setState(code === "denied" ? "denied" : state);
      toast.error(t(`push.error.${code}`) || (e as Error).message);
    },
  });

  const disable = useMutation({
    mutationFn: () => disablePush((body) => pushApi.unsubscribe(token!, body)),
    onSuccess: () => { setState("ready"); void cfg.refetch(); toast.success(t("push.disabled")); },
    onError: (e) => toast.error((e as Error).message),
  });

  const test = useMutation({
    mutationFn: () => pushApi.test(token!),
    onSuccess: () => toast.success(t("push.testSent")),
    onError: (e) => toast.error((e as Error).message),
  });

  // The server has no VAPID keys: nothing the user does in the browser can
  // help, so say that instead of offering a button that always fails.
  const serverOff = cfg.data && !cfg.data.enabled;
  const blocked: PushState[] = ["insecure", "ios-tab", "denied", "unsupported"];
  const problem = serverOff ? "server-off" : (state && blocked.includes(state) ? state : null);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <BellIcon className="h-4 w-4" /> {t("push.title")}
        </CardTitle>
        <CardDescription>{t("push.desc")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {problem && (
          <p className="text-sm text-[var(--color-muted-foreground)]">
            {t(`push.error.${problem}`)}
          </p>
        )}

        {!problem && state === "subscribed" && (
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => disable.mutate()}
                    disabled={disable.isPending}>
              <BellOffIcon className="mr-1 h-3.5 w-3.5" /> {t("push.disable")}
            </Button>
            {/* Between a worker, a permission, a VAPID pair and a push
                service, a silent failure has four plausible causes — one
                button that either buzzes or errors settles it. */}
            <Button variant="ghost" size="sm" onClick={() => test.mutate()}
                    disabled={test.isPending}>
              {test.isPending
                ? <Loader2Icon className="mr-1 h-3.5 w-3.5 animate-spin" />
                : null}
              {t("push.test")}
            </Button>
          </div>
        )}

        {!problem && state === "ready" && (
          <Button size="sm" onClick={() => enable.mutate()} disabled={enable.isPending}>
            {enable.isPending
              ? <Loader2Icon className="mr-1 h-3.5 w-3.5 animate-spin" />
              : <BellIcon className="mr-1 h-3.5 w-3.5" />}
            {t("push.enable")}
          </Button>
        )}

        {cfg.data && cfg.data.devices > 0 && (
          <p className="text-xs text-[var(--color-muted-foreground)]">
            {t("push.devices", { count: cfg.data.devices })}
          </p>
        )}
        {!isStandalone() && state !== "ios-tab" && !problem && (
          <p className="text-xs text-[var(--color-muted-foreground)]">
            {t("push.installHint")}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
