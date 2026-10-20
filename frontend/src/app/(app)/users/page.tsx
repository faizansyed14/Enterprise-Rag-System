"use client";

import { Pencil, Search, UserPlus } from "lucide-react";
import { useEffect, useState } from "react";

import { BrandIcon } from "@/components/brand/brand";
import { Button } from "@/components/ui/button";
import { Badge, ErrorNote, Spinner, StatusBadge } from "@/components/ui/feedback";
import { Field, Input, Switch } from "@/components/ui/fields";
import { PageHeader, Table, Td, Th } from "@/components/ui/layout";
import { Dialog } from "@/components/ui/overlay";
import { useToast } from "@/components/ui/toast";
import { api, errorMessage } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";

type UserRow = {
  id: string;
  email: string;
  display_name: string;
  auth_provider: string;
  is_admin: boolean;
  is_active: boolean;
  created_at: string;
  last_login_at: string | null;
  groups: { principal: string; source: string }[];
};

const groupLabel = (p: string) => (p.startsWith("lgroup:") ? p.slice(7) : p.startsWith("group:") ? `Entra ${p.slice(6, 14)}…` : p);

/** Mounted while open (keyed by the user being edited), so the fields start from that user. */
function UserDialog({ user, onClose, onSaved }: { user: UserRow | null; onClose: () => void; onSaved: () => void }) {
  const toast = useToast();
  const { user: me } = useSession();
  const [email, setEmail] = useState(user?.email ?? "");
  const [name, setName] = useState(user?.display_name ?? "");
  const [groups, setGroups] = useState(
    (user?.groups ?? [])
      .filter((g) => g.source === "local")
      .map((g) => groupLabel(g.principal))
      .join(", "),
  );
  const [password, setPassword] = useState("");
  const [admin, setAdmin] = useState(user?.is_admin ?? false);
  const [active, setActive] = useState(user?.is_active ?? true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const self = user?.id === me.id;
  const save = async () => {
    setBusy(true);
    setError(null);
    const groupList = groups.split(",").map((g) => g.trim()).filter(Boolean);
    try {
      if (user) {
        await api(`/users/${user.id}`, {
          method: "PATCH",
          body: { display_name: name, groups: groupList, is_admin: admin, is_active: active, ...(password ? { password } : {}) },
        });
        toast("User saved");
      } else {
        await api("/users", { body: { email: email.trim(), display_name: name, password, is_admin: admin, groups: groupList } });
        toast("User added");
      }
      onSaved();
      onClose();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={user ? `Edit ${user.display_name || user.email}` : "Add a user"}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" onClick={save} disabled={busy || (!user && (!email.trim() || password.length < 12)) || (password.length > 0 && password.length < 12)}>
            {busy ? "Saving" : user ? "Save" : "Add user"}
          </Button>
        </>
      }
    >
      {!user ? (
        <Field label="E-mail">
          <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="off" />
        </Field>
      ) : null}
      <Field label="Name">
        <Input value={name} onChange={(e) => setName(e.target.value)} />
      </Field>
      <Field label="Groups" hint="Comma-separated. Documents shared with a group (lgroup:name) become visible to its members.">
        <Input value={groups} onChange={(e) => setGroups(e.target.value)} placeholder="commercial, site-team" />
      </Field>
      {user?.auth_provider !== "entra" ? (
        <Field label={user ? "New password (optional)" : "Password"} hint="At least 12 characters. Changing it signs the user out everywhere.">
          <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
        </Field>
      ) : (
        <p className="text-[12.5px] text-muted">Signs in with Microsoft; Entra groups are synced at sign-in and are not edited here.</p>
      )}
      <div className="flex flex-wrap gap-6 text-[13px]">
        <label className="flex items-center gap-2">
          <Switch checked={admin} onChange={setAdmin} label="Administrator" disabled={self} />
          Administrator
        </label>
        {user ? (
          <label className="flex items-center gap-2">
            <Switch checked={active} onChange={setActive} label="Can sign in" disabled={self} />
            Can sign in
          </label>
        ) : null}
      </div>
      {self ? <p className="text-[12px] text-muted">You cannot remove your own administrator rights or switch yourself off.</p> : null}
      {error ? <ErrorNote>{error}</ErrorNote> : null}
    </Dialog>
  );
}

export default function UsersPage() {
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [dialog, setDialog] = useState<{ open: boolean; user: UserRow | null }>({ open: false, user: null });
  useEffect(() => {
    document.title = "Users";
  }, []);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(query.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [query]);
  const { data, error, reload } = useApi<{ total: number; users: UserRow[] }>(`/users?${new URLSearchParams(debounced ? { q: debounced } : {})}`);

  return (
    <div>
      <PageHeader
        title="Users"
        description="Local accounts and people who signed in with Microsoft. Groups decide which uploaded and shared documents each person sees."
        actions={
          <Button variant="primary" icon={<UserPlus size={15} aria-hidden />} onClick={() => setDialog({ open: true, user: null })}>
            Add user
          </Button>
        }
      />
      <div className="space-y-4 px-6 py-6 lg:px-10">
        <div className="relative max-w-sm">
          <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" aria-hidden />
          <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search by name or e-mail" className="pl-9" aria-label="Search users" />
        </div>
        {error ? <ErrorNote>{error}</ErrorNote> : null}
        {!data && !error ? <Spinner label="Loading users" /> : null}
        {data ? (
          <div className="rounded-2xl border border-border">
            <Table>
              <thead>
                <tr>
                  <Th>Person</Th>
                  <Th>Signs in with</Th>
                  <Th>Groups</Th>
                  <Th>Last sign-in</Th>
                  <Th>State</Th>
                  <Th />
                </tr>
              </thead>
              <tbody>
                {data.users.map((u) => (
                  <tr key={u.id}>
                    <Td>
                      <div className="flex items-center gap-2 font-medium">
                        {u.display_name || u.email}
                        {u.is_admin ? <Badge tone="outline">Administrator</Badge> : null}
                      </div>
                      <div className="text-[12.5px] text-muted">{u.email}</div>
                    </Td>
                    <Td>
                      <span className="inline-flex items-center gap-2">
                        {u.auth_provider === "entra" ? <BrandIcon name="microsoft" size={14} tile={false} /> : null}
                        {u.auth_provider === "entra" ? "Microsoft" : "Password"}
                      </span>
                    </Td>
                    <Td className="max-w-[280px]">
                      <div className="flex flex-wrap gap-1">
                        {u.groups.length ? (
                          u.groups.slice(0, 6).map((g) => (
                            <Badge key={g.principal} title={`${g.principal} (${g.source})`}>
                              {groupLabel(g.principal)}
                            </Badge>
                          ))
                        ) : (
                          <span className="text-muted">—</span>
                        )}
                        {u.groups.length > 6 ? <Badge>+{u.groups.length - 6}</Badge> : null}
                      </div>
                    </Td>
                    <Td className="whitespace-nowrap text-muted">{u.last_login_at ? relativeTime(u.last_login_at) : "Never"}</Td>
                    <Td>{u.is_active ? <StatusBadge state="ok">Active</StatusBadge> : <StatusBadge state="fail">Off</StatusBadge>}</Td>
                    <Td align="right">
                      <Button size="sm" variant="ghost" icon={<Pencil size={14} aria-hidden />} onClick={() => setDialog({ open: true, user: u })}>
                        Edit
                      </Button>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </div>
        ) : null}
      </div>
      {dialog.open ? <UserDialog key={dialog.user?.id ?? "new"} user={dialog.user} onClose={() => setDialog({ open: false, user: null })} onSaved={reload} /> : null}
    </div>
  );
}
