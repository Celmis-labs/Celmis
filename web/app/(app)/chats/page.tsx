"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { MessagesSquareIcon } from "lucide-react";

import { chatsApi, type ChatOut } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { QueryState } from "@/components/ui/query-state";
import { WorkspaceBadge } from "@/components/workspace-badge";

export default function ChatsIndexPage() {
  const token = useToken();
  const t = useT();

  // All chats (across projects + single-repo)
  const chats = useQuery({
    queryKey: ["chats", "all"],
    queryFn: () => chatsApi.list(token!),
    enabled: !!token,
  });

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<MessagesSquareIcon className="h-6 w-6" />}
        title={t("chats.heading")}
        badge={<WorkspaceBadge />}
        description={t("chats.description")}
        tabs={<SectionTabs set="qa" />}
      />

      <QueryState
        query={chats}
        empty={{
          icon: MessagesSquareIcon,
          title: t("chats.empty"),
          action: (
            <Link href="/projects" className="text-sm underline text-primary">
              {t("chats.createProject")}
            </Link>
          ),
        }}
      >
        {(data) => (
          <div className="space-y-2">
            {data.map((c) => (
              <ChatCard key={c.id} chat={c} />
            ))}
          </div>
        )}
      </QueryState>
    </PageShell>
  );
}

function ChatCard({ chat: c }: { chat: ChatOut }) {
  const t = useT();
  const body = (
    <CardContent className="py-3 px-4">
      <div className="flex-1 min-w-0">
        <div className="font-medium text-sm truncate">
          {c.name || t("chats.untitledChat", { id: c.id.slice(0, 8) })}
        </div>
        <div className="flex items-center gap-2 mt-1 text-xs text-muted-foreground">
          {c.project_id ? (
            <Badge variant="outline" className="text-[10px]">
              {t("chats.projectBadge")}
            </Badge>
          ) : (
            <Badge variant="warning" className="text-[10px]">
              {t("chats.legacyNoProject")}
            </Badge>
          )}
          {c.repo_slug && (
            <Badge variant="outline" className="text-[10px]">
              {c.repo_slug}
            </Badge>
          )}
          <span>· {t("chats.messagesCount", { count: c.messages_count })}</span>
          <span>· {new Date(c.updated_at).toLocaleString()}</span>
        </div>
      </div>
    </CardContent>
  );

  // Legacy single-repo chats have no project — and no route to open them
  // (/chats/{id} does not exist), so they render as a plain, non-clickable row.
  if (!c.project_id) {
    return <Card className="opacity-80">{body}</Card>;
  }

  return (
    <Link href={`/projects/${c.project_id}/chats/${c.id}`}>
      <Card className="hover:shadow-md transition-shadow cursor-pointer">{body}</Card>
    </Link>
  );
}
