import { useEffect, useState } from "react";
import { getUser, USER_CHANGED_EVENT } from "@/lib/api";

// The user lives in localStorage (no auth context); this re-renders when it is updated.
export default function useCurrentUser() {
  const [user, setUser] = useState(getUser);
  useEffect(() => {
    const sync = () => setUser(getUser());
    window.addEventListener(USER_CHANGED_EVENT, sync);
    return () => window.removeEventListener(USER_CHANGED_EVENT, sync);
  }, []);
  return user;
}
