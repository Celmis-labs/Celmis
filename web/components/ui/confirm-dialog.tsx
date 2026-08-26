"use client";

import { useCallback, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { useT } from "@/lib/i18n";

export type ConfirmOptions = {
  title: string;
  description?: string;
  confirmLabel?: string;
  /** Renders the confirm button in the destructive variant. */
  danger?: boolean;
};

/**
 * Promise-based replacement for window.confirm.
 *
 * Usage:
 *   const { confirm, dialog } = useConfirm();
 *   ...
 *   if (await confirm({ title: t("..."), danger: true })) mut.mutate(id);
 *   ...
 *   return <div>...{dialog}</div>;   // render once per page
 */
export function useConfirm(): {
  confirm: (opts: ConfirmOptions) => Promise<boolean>;
  dialog: React.ReactNode;
} {
  const t = useT();
  const [opts, setOpts] = useState<ConfirmOptions | null>(null);
  const resolver = useRef<((v: boolean) => void) | null>(null);

  const confirm = useCallback((o: ConfirmOptions) => {
    return new Promise<boolean>((resolve) => {
      // If a previous dialog is somehow still open, treat it as cancelled.
      resolver.current?.(false);
      resolver.current = resolve;
      setOpts(o);
    });
  }, []);

  const close = (result: boolean) => {
    resolver.current?.(result);
    resolver.current = null;
    setOpts(null);
  };

  const dialog = (
    <Dialog open={!!opts} onOpenChange={(open) => { if (!open) close(false); }}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{opts?.title}</DialogTitle>
          {opts?.description && <DialogDescription>{opts.description}</DialogDescription>}
        </DialogHeader>
        <DialogFooter>
          <Button variant="ghost" onClick={() => close(false)}>
            {t("common.cancel")}
          </Button>
          <Button
            autoFocus
            variant={opts?.danger ? "destructive" : "default"}
            onClick={() => close(true)}
          >
            {opts?.confirmLabel ?? t("common.confirm")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );

  return { confirm, dialog };
}
