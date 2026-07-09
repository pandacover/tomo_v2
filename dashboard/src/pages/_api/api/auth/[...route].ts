import { getAuth } from '../../../../lib/auth';

export const GET = async (request: Request): Promise<Response> => (await getAuth()).handler(request);
export const POST = async (request: Request): Promise<Response> => (await getAuth()).handler(request);
