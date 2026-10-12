"use client";
export default function ErrorPage({ reset }: { reset: () => void }) {
  return (
    <main className="center-state">
      <h1>Something interrupted this page.</h1>
      <p>Your saved data has not been changed.</p>
      <button className="button primary" onClick={reset}>
        Try again
      </button>
    </main>
  );
}
