import type { Metadata } from "next";
import { connection } from "next/server";
import { Providers } from "@/components/providers";
import "./globals.css";
import "./workforce.css";
export const metadata: Metadata = {
  title: "Meridian · Workforce Intelligence",
  description:
    "Independent, synthetic-data workforce intelligence and operations.",
};
export default async function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  await connection();
  return (
    <html lang="en">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
