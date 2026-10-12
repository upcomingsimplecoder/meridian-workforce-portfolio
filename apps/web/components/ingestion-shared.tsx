export function Failure({
  error,
  retry,
}: {
  error: Error;
  retry?: () => void;
}) {
  return (
    <div className="error-box" role="alert">
      <h3>Request could not be completed</h3>
      <p>{error.message}</p>
      {retry && (
        <button className="button" onClick={retry}>
          Retry request
        </button>
      )}
    </div>
  );
}
