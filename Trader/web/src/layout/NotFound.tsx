// Any path the app does not know (P4-T12).
import { Link } from "react-router-dom";

import { Card } from "../components/ui";

export function NotFound() {
  return (
    <main className="page">
      <h1>Not found</h1>
      <Card>
        <p>There is no page here.</p>
        <Link to="/dashboard">Go to the dashboard</Link>
      </Card>
    </main>
  );
}

export default NotFound;
