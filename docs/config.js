// Fill these in before deploying. All three are SAFE to expose in
// client-side code:
//  - SUPABASE_URL / SUPABASE_ANON_KEY: the anon key is meant to be
//    public. Row-Level Security (see supabase_app_additions.sql)
//    restricts it to reading only today's pre-generated slot content
//    and inserting its own push subscription - nothing else.
//  - VAPID_PUBLIC_KEY: public half of the Web Push keypair, meant to be
//    public (the PRIVATE half stays server-side only, as a GitHub
//    secret - never put it here).

const CONFIG = {
  SUPABASE_URL: "https://saxbibqukwvooyqnsfeq.supabase.co/",
  SUPABASE_ANON_KEY: "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InNheGJpYnF1a3d2b295cW5zZmVxIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODU1OTM2NTUsImV4cCI6MjEwMTE2OTY1NX0.dleFGdlcwnprqIOxewSMJDXvPOYWeN0VytRfrXL7hyU",
  VAPID_PUBLIC_KEY: "BBe0ooMu2Qe13ybWgKPhEIOb5EzUhds5bwGIUowW1498a3lbIRICPn_iOHxWfqBvCMZvRZG6gzUTIYDyRtktW0w",
};
